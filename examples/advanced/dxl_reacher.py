#!/usr/bin/env python
# Copyright (c) 2018, The SenseAct Authors.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
import argparse
import copy
import time
from multiprocessing import Process, Value, Manager

import baselines.common.tf_util as U
import numpy as np
from baselines.ppo1.mlp_policy import MlpPolicy
from baselines.trpo_mpi.trpo_mpi import learn
from helper import create_callback

from senseact.devices.dxl import dxl_communicator as gcomm
from senseact.envs.dxl.dxl_reacher_env import DxlReacher1DEnv
from senseact.utils import tf_set_seeds, NormalizedEnv


def main(port: str, id: int, baud: int):
    tag = str(time.time())

    # use fixed random state
    rand_state = np.random.RandomState(1).get_state()
    np.random.set_state(rand_state)
    tf_set_seeds(np.random.randint(1, 2 ** 31 - 1))

    obs_history = 1
    comm_name = "DXL"
    communicator_setups = {
        comm_name: {
            'Communicator': gcomm.DXLCommunicator,
            'num_sensor_packets': obs_history,
            'kwargs': {
                "idn": id,
                "baudrate": baud,
                "sensor_dt": 0.01,
                "device_path": port,
                "use_ctypes_driver": True
            }
        }
    }

    # Create DXL Reacher1D environment
    env = DxlReacher1DEnv(setup='dxl_gripper_default',
                          communicator_setups=communicator_setups,
                          actuator_name=comm_name,
                          sensor_name=comm_name,
                          obs_history=obs_history,
                          dt=0.04,
                          rllab_box=False,
                          episode_length_step=None,
                          episode_length_time=2,
                          max_torque_mag=100,
                          control_type='torque',
                          target_type='position',
                          reset_type='zero',
                          reward_type='linear',
                          random_state=rand_state
                          )

    # The outputs of the policy function are sampled from a Gaussian. However, the actions in terms of torque
    # commands are in the range [-max_torque_mag, max_torque_mag]. NormalizedEnv wrapper scales action accordingly.
    # By default, it does not normalize observations or rewards.
    env = NormalizedEnv(env)

    # Start environment processes
    env.start()

    # Create baselines TRPO policy function
    sess = U.single_threaded_session()
    sess.__enter__()

    def policy_fn(name, ob_space, ac_space):
        return MlpPolicy(name=name, ob_space=ob_space, ac_space=ac_space,
                         hid_size=32, num_hid_layers=2)

    # create and start plotting process
    plot_running = Value('i', 1)
    shared_returns = Manager().dict({"write_lock": False,
                                     "episodic_returns": [],
                                     "episodic_lengths": [], })
    # Plotting process
    pp = Process(target=plot_dxl_reacher, args=(tag, env, 2048, shared_returns, plot_running))
    pp.start()

    # Create callback function for logging data from baselines TRPO learn
    kindred_callback = create_callback(shared_returns)

    # Train baselines TRPO
    learn(env, policy_fn,
          max_timesteps=50000,
          timesteps_per_batch=2048,
          max_kl=0.05,
          cg_iters=10,
          cg_damping=0.1,
          vf_iters=5,
          vf_stepsize=0.001,
          gamma=0.995,
          lam=0.995,
          callback=kindred_callback,
          )

    # Safely terminate plotter process
    plot_running.value = 0  # shutdown ploting process
    time.sleep(2)
    pp.join()

    # Shutdown the environment
    env.close()

def plot_dxl_reacher(tag, env, batch_size, shared_returns, plot_running):
    """ Visualizes the DXL reacher task and plots episodic returns

    Args:
        env: An instance of DxlReacher1DEnv
        batch_size: An int representing timesteps_per_batch provided to the PPO learn function
        shared_returns: A manager dictionary object containing `episodic returns` and `episodic lengths`
        plot_running: A multiprocessing Value object containing 0/1.
            1: Continue plotting, 0: Terminate plotting loop
    """
    print("Started plotting routine")
    import matplotlib.pyplot as plt
    plt.ion()
    time.sleep(5.0)
    fig = plt.figure(figsize=(20, 6))
    ax1 = fig.add_subplot(121)
    hl1, = ax1.plot([], [], markersize=10, marker="o", color='r')
    hl2, = ax1.plot([], [], markersize=10, marker="o", color='b')
    ax1.set_xlabel("X")
    ax1.set_ylabel("Y")
    ax2 = fig.add_subplot(122)
    hl11, = ax2.plot([], [])
    fig.suptitle("DXL Reacher", fontsize=14)
    ax2.set_title("Learning Curve")
    ax2.set_xlabel("Time Step")
    ax2.set_ylabel("Average Returns")
    count = 0

    old_size = len(shared_returns['episodic_returns'])
    while plot_running.value:
        hl1.set_ydata([1])
        hl1.set_xdata([env._target_pos_.value])
        hl2.set_ydata([1])
        hl2.set_xdata([env._present_pos_[-1]])
        ax1.set_ylim([0, 2])
        ax1.set_xlim([env.angle_low, env.angle_high])
        ax1.set_title("Current Reward: " + str(env._reward_.value))
        ax1.set_xlim(ax1.get_xlim()[::-1])
        ax1.set_ylim(ax1.get_ylim()[::-1])

        # make a copy of the whole dict to avoid episode_returns and episodic_lengths getting desync
        # while plotting
        copied_returns = copy.deepcopy(shared_returns)
        if not copied_returns['write_lock'] and len(copied_returns['episodic_returns']) > old_size:
            # plot learning curve
            returns = np.array(copied_returns['episodic_returns'])
            old_size = len(copied_returns['episodic_returns'])
            window_size_steps = 5000
            x_tick = 1000

            if copied_returns['episodic_lengths']:
                ep_lens = np.array(copied_returns['episodic_lengths'])
            else:
                ep_lens = batch_size * np.arange(len(returns))
            cum_episode_lengths = np.cumsum(ep_lens)

            if cum_episode_lengths[-1] >= x_tick:
                steps_show = np.arange(x_tick, cum_episode_lengths[-1] + 1, x_tick)
                rets = []

                for i in range(len(steps_show)):
                    rets_in_window = returns[(cum_episode_lengths > max(0, x_tick * (i + 1) - window_size_steps)) *
                                             (cum_episode_lengths < x_tick * (i + 1))]
                    if rets_in_window.any():
                        rets.append(np.mean(rets_in_window))

                hl11.set_xdata(np.arange(1, len(rets) + 1) * x_tick)
                ax2.set_xlim([x_tick, len(rets) * x_tick])
                hl11.set_ydata(rets)
                buffer = abs(np.max(rets) - np.min(rets)) * 0.05
                ax2.set_ylim([np.min(rets) - buffer, np.max(rets) + buffer])
        time.sleep(0.01)
        fig.canvas.draw()
        fig.canvas.flush_events()
        count += 1

    # Save just the portion _inside_ the second axis's boundaries
    extent = ax2.get_window_extent().transformed(fig.dpi_scale_trans.inverted())
    fig.savefig('{}.png'.format(tag), bbox_inches=extent.expanded(1.1, 1.1))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=str, default=None)
    parser.add_argument("--id", type=int, default=1)
    parser.add_argument("--baud", type=int, default=1000000)
    args = parser.parse_args()

    main(**args.__dict__)
