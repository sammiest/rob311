import sys
import threading
import time
import numpy as np
from threading import Thread
from MBot.Messages.message_defs import mo_states_dtype, mo_cmds_dtype, mo_pid_params_dtype
from MBot.SerialProtocol.protocol import SerialProtocol
from DataLogger import dataLogger

# ---------------------------------------------------------------------------
"""
ROB 311 - Ball-bot kinetics and kinematics demo

This program uses a soft realtime loop to enforce loop timing. Soft real time loop is a  class
designed to allow clean exits from infinite loops with the potential for post-loop cleanup operations executing.

The Loop Killer object watches for the key shutdown signals on the UNIX operating system (which runs on the PI)
when it detects a shutdown signal, it sets a flag, which is used by the Soft Realtime Loop to stop iterating.
Typically, it detects the CTRL-C from your keyboard, which sends a SIGTERM signal.

the function_in_loop argument to the Soft Realtime Loop's blocking_loop method is the function to be run every loop.
A typical usage would set function_in_loop to be a method of an object, so that the object could store program state.
See the 'ifmain' for two examples.

Authors: Senthur Raj, Gray Thomas, Yves Nazon, Japman Gill and Elliott Rouse 
Neurobionics Lab / Locomotor Control Lab
"""

import signal
import time
from math import sqrt

PRECISION_OF_SLEEP = 0.0001

# Version of the SoftRealtimeLoop library
__version__ = "1.0.0"

class LoopKiller:
    def __init__(self, fade_time=0.0):
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGHUP, self.handle_signal)
        self._fade_time = fade_time
        self._soft_kill_time = None

    def handle_signal(self, signum, frame):
        self.kill_now = True

    def get_fade(self):
        # interpolates from 1 to zero with soft fade out
        if self._kill_soon:
            t = time.time() - self._soft_kill_time
            if t >= self._fade_time:
                return 0.0
            return 1.0 - (t / self._fade_time)
        return 1.0

    _kill_now = False
    _kill_soon = False

    @property
    def kill_now(self):
        if self._kill_now:
            return True
        if self._kill_soon:
            t = time.time() - self._soft_kill_time
            if t > self._fade_time:
                self._kill_now = True
        return self._kill_now

    @kill_now.setter
    def kill_now(self, val):
        if val:
            if self._kill_soon:  # if you kill twice, then it becomes immediate
                self._kill_now = True
            else:
                if self._fade_time > 0.0:
                    self._kill_soon = True
                    self._soft_kill_time = time.time()
                else:
                    self._kill_now = True
        else:
            self._kill_now = False
            self._kill_soon = False
            self._soft_kill_time = None

class SoftRealtimeLoop:
    def __init__(self, dt=0.001, report=False, fade=0.0):
        self.t0 = self.t1 = time.time()
        self.killer = LoopKiller(fade_time=fade)
        self.dt = dt
        self.ttarg = None
        self.sum_err = 0.0
        self.sum_var = 0.0
        self.sleep_t_agg = 0.0
        self.n = 0
        self.report = report

    def __del__(self):
        if self.report:
            print("In %d cycles at %.2f Hz:" % (self.n, 1.0 / self.dt))
            print("\tavg error: %.3f milliseconds" % (1e3 * self.sum_err / self.n))
            print(
                "\tstddev error: %.3f milliseconds"
                % (
                    1e3
                    * sqrt((self.sum_var - self.sum_err**2 / self.n) / (self.n - 1))
                )
            )
            print(
                "\tpercent of time sleeping: %.1f %%"
                % (self.sleep_t_agg / self.time() * 100.0)
            )

    @property
    def fade(self):
        return self.killer.get_fade()

    def run(self, function_in_loop, dt=None):
        if dt is None:
            dt = self.dt
        self.t0 = self.t1 = time.time() + dt
        while not self.killer.kill_now:
            ret = function_in_loop()
            if ret == 0:
                self.stop()
            while time.time() < self.t1 and not self.killer.kill_now:
                if signal.sigtimedwait(
                    [signal.SIGTERM, signal.SIGINT, signal.SIGHUP], 0
                ):
                    self.stop()
            self.t1 += dt
        print("Soft realtime loop has ended successfully.")

    def stop(self):
        self.killer.kill_now = True

    def time(self):
        return time.time() - self.t0

    def time_since(self):
        return time.time() - self.t1

    def __iter__(self):
        self.t0 = self.t1 = time.time() + self.dt
        return self

    def __next__(self):
        if self.killer.kill_now:
            raise StopIteration

        while (
            time.time() < self.t1 - 2 * PRECISION_OF_SLEEP and not self.killer.kill_now
        ):
            t_pre_sleep = time.time()
            time.sleep(
                max(PRECISION_OF_SLEEP, self.t1 - time.time() - PRECISION_OF_SLEEP)
            )
            self.sleep_t_agg += time.time() - t_pre_sleep

        while time.time() < self.t1 and not self.killer.kill_now:
            if signal.sigtimedwait([signal.SIGTERM, signal.SIGINT, signal.SIGHUP], 0):
                self.stop()
        if self.killer.kill_now:
            raise StopIteration
        self.t1 += self.dt
        if self.ttarg is None:
            # inits ttarg on first call
            self.ttarg = time.time() + self.dt
            # then skips the first loop
            return self.t1 - self.t0
        error = time.time() - self.ttarg  # seconds
        self.sum_err += error
        self.sum_var += error**2
        self.n += 1
        self.ttarg += self.dt
        return self.t1 - self.t0

# ---------------------------------------------------------------------------

def register_topics(ser_dev:SerialProtocol):
    ser_dev.serializer_dict[101] = [lambda bytes: np.frombuffer(bytes, dtype=mo_cmds_dtype), lambda data: data.tobytes()]
    ser_dev.serializer_dict[121] = [lambda bytes: np.frombuffer(bytes, dtype=mo_states_dtype), lambda data: data.tobytes()]

# ---------------------------------------------------------------------------

FREQ = 200
DT = 1/FREQ

RW = 0.048
RK = 0.1210
ALPHA = np.deg2rad(45)

# ---------------------------------------------------------------------------

def compute_motor_torques(Tx, Ty, Tz):
    '''
    Parameters:
    ----------
    Tx: Torque along x-axis
    Ty: Torque along y-axis
    Tz: Torque along z-axis

    Returns:
    --------
            Ty
            T1
            |
            |
            |
            . _ _ _ _ Tx
           / \
          /   \
         /     \
        /       \
       T2       T3

    T1: Motor Torque 1
    T2: Motor Torque 2
    T3: Motor Torque 3
    '''

    # ---------------------------------------------------------
    # LAB 8
    # YOUR CODE GOES HERE
    T1 = (1/3) * (Tz - ((2*Ty)/np.cos(ALPHA)))
    T2 = (1/3) * (Tz + ((1/np.cos(ALPHA)) * (-np.sqrt(3) * Tx + Ty)))
    T3 = (1/3) * (Tz + ((1/np.cos(ALPHA)) * (np.sqrt(3) * Tx + Ty)))
    # ---------------------------------------------------------

    return T1, T2, T3

# ---------------------------------------------------------------------------


def compute_phi(psi_1, psi_2, psi_3):
    '''
    Parameters:
    ----------
    psi_1: Encoder reading (rad) [MOTOR 1]
    psi_2: Encoder reading (rad) [MOTOR 2]
    psi_3: Encoder reading (rad) [MOTOR 3]

    Returns:
    --------
    phi_x: Ball rotation along x-axis (rad)
    phi_y: Ball rotation along y-axis (rad)
    phi_z: Ball rotation along z-axis (rad)
    '''

    # ---------------------------------------------------------
    # LAB 9
    # YOUR CODE GOES HERE
    phi_x = np.sqrt(2/3) * (RW / RK) * (psi_2 - psi_3)
    phi_y = (np.sqrt(2)/3) * (RW / RK) * (-2 * psi_1 + psi_2 + psi_3)
    phi_z = (np.sqrt(2)/3) * (RW / RK) * (psi_1 + psi_2 + psi_3)
    # ---------------------------------------------------------

    return phi_x, phi_y, phi_z

# ---------------------------------------------------------------------------

if __name__ == "__main__":
    trial_num = int(input('Trial Number? '))
    filename = 'ROB311_Test%i' % trial_num
    dl = dataLogger(filename + '.txt')

    ser_dev = SerialProtocol()
    register_topics(ser_dev)

    # Init serial
    serial_read_thread = Thread(target = SerialProtocol.read_loop, args=(ser_dev,), daemon=True)
    serial_read_thread.start()

    # Local structs
    commands = np.zeros(1, dtype=mo_cmds_dtype)[0]
    states = np.zeros(1, dtype=mo_states_dtype)[0]

    theta_roll = 0.0
    theta_pitch = 0.0

    psi_1 = 0.0
    psi_2 = 0.0
    psi_3 = 0.0

    phi_x = 0.0
    phi_y = 0.0
    phi_z = 0.0

    # Motor torques
    T1 = 0.0
    T2 = 0.0
    T3 = 0.0

    commands['start'] = 1.0

    # Time for comms to sync
    time.sleep(1.0)

    ser_dev.send_topic_data(101, commands)

    print('Beginning program!')
    i = 0

    for t in SoftRealtimeLoop(dt=DT, report=True):
        try:
            states = ser_dev.get_cur_topic_data(121)[0]
            if i == 0:
                t_start = time.time()
            i = i + 1
        except KeyError as e:
            continue
        t_now = time.time() - t_start

        # Define variables for saving / analysis here - below you can create variables from the available states
        
        # Body lean angles
        theta_x = (states['theta_roll'])  
        theta_y = (states['theta_pitch'])
        
        # ---------------------------------------------------------
        # LAB 7
        # Wheel Rotations

        # YOUR CODE GOES HERE
        # ---------------------------------------------------------
        psi_1 = (states['psi_1'])
        psi_2 = (states['psi_2'])
        psi_3 = (states['psi_3'])
        # ---------------------------------------------------------
        # LAB 8
        # Compute motor torques (T1, T2, and T3) with Tx, Ty, and Tz
        # Beginning with ball rolling toward positive y-axis
        # CHANGE THESE TO ADJUST THE ROLLING DIRECTION OF YOUR BALL-BOT
        Tx = .5 * np.sqrt(2) / 2
        Ty = -.5 * np.sqrt(2) / 2
        Tz = 0

        T1, T2, T3 = compute_motor_torques(Tx, Ty, Tz)
        # T1 =1
        # T2 = 1
        # T3 = 1
        # ---------------------------------------------------------

        print("T1: {}, T2: {}, T3: {}".format(T1, T2, T3))
        commands['motor_1_duty'] = T1
        commands['motor_2_duty'] = T2
        commands['motor_3_duty'] = T3  

        # ---------------------------------------------------------
        # LAB 9
        # Compute ball rotation (phi) with psi_1, psi_2, and psi_3

        phi_x, phi_y, phi_z = compute_phi(psi_1, psi_2, psi_3)
        print("PHI X: {}, PHI Y: {}, PHI Z: {}".format(phi_x, phi_y, phi_z))
        # ---------------------------------------------

        # ---------------------------------------------
        # LAB 9
        # Construct the data matrix for saving - you can add more variables by replicating the format below
        # Append the following variables to the data variable:
        # motor torques - T1, T2, T3
        # ball rotations - phi_x, phi_y, phi_z
        # wheel rotations - psi_1, psi_2, psi_3
 
        data = [i, t_now, theta_x, theta_y, T1, T2, T3, phi_x, phi_y, phi_z, psi_1, psi_2, psi_3]
        # ---------------------------------------------

        dl.appendData(data)

        ser_dev.send_topic_data(101, commands)
    
    print("Saving data...")
    dl.writeOut()

    print("Resetting Motor commands.")
    time.sleep(0.25)
    commands['start'] = 0.0
    time.sleep(0.25)
    commands['motor_1_duty'] = 0.0
    commands['motor_2_duty'] = 0.0
    commands['motor_3_duty'] = 0.0
    time.sleep(0.25)
    ser_dev.send_topic_data(101, commands)