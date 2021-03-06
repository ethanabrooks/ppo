import inspect
import itertools
import os
from collections import namedtuple, Counter
from multiprocessing import Queue
from pathlib import Path
from pprint import pprint
from typing import Dict, Optional

import gym
import hydra
import numpy as np
import torch
import torch.nn as nn
from hydra.core.config_store import ConfigStore
from omegaconf import DictConfig

import wandb
from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv

from agents import Agent, AgentOutputs, MLPBase
from aggregator import (
    EpisodeAggregator,
    InfosAggregator,
    TotalTimeKeeper,
    AverageTimeKeeper,
    EvalEpisodeAggregator,
    EvalInfosAggregator,
)
from config import Config, flatten
from ppo import PPO
from rollouts import RolloutStorage
from wrappers import VecPyTorch

EpochOutputs = namedtuple("EpochOutputs", "obs reward done infos act masks")
CHECKPOINT_NAME = "checkpoint.pt"


class Trainer:
    @classmethod
    def args_to_methods(cls):
        return dict(
            agent_args=[
                cls.build_agent,
                Agent.__init__,
                MLPBase.__init__,
            ],
            curriculum_args=[cls.initialize_curriculum],
            failure_buffer_args=[cls.build_failure_buffer],
            rollouts_args=[RolloutStorage.__init__],
            ppo_args=[PPO.__init__],
            env_args=[cls.make_env, cls.make_vec_envs],
            run_args=[cls.run],
        )

    @staticmethod
    def build_agent(envs, activation=nn.ReLU(), **agent_args):
        return Agent(
            envs.observation_space.shape,
            envs.action_space,
            activation=activation,
            **agent_args,
        )

    @classmethod
    def build_failure_buffer(cls, **kwargs) -> Optional[Queue]:
        pass

    @staticmethod
    def build_infos_aggregator() -> InfosAggregator:
        return InfosAggregator()

    @classmethod
    def dump_failure_buffer(cls, failure_buffer, log_dir: Path):
        pass

    @classmethod
    def initialize_curriculum(cls, **kwargs):
        while True:
            yield

    @staticmethod
    def load_checkpoint(checkpoint_path, ppo, agent, device):
        state_dict = torch.load(str(checkpoint_path), map_location=device)
        agent.load_state_dict(state_dict["agent"])
        ppo.optimizer.load_state_dict(state_dict["optimizer"])
        print(f"Loaded parameters from {checkpoint_path}.")
        return state_dict.get("step", -1) + 1

    @classmethod
    def make_vec_envs(
        cls,
        evaluating: bool,
        num_processes: int,
        render: bool,
        synchronous: bool,
        log_dir=None,
        mp_kwargs: dict = None,
        **kwargs,
    ) -> VecPyTorch:
        if mp_kwargs is None:
            mp_kwargs = {}

        if num_processes == 1:
            synchronous = True

        if synchronous:
            kwargs.update(mp_kwargs)

        def env_thunk(rank):
            def thunk(**_kwargs):
                return cls.make_env(
                    rank=rank, evaluating=evaluating, **_kwargs, **kwargs
                )

            return thunk

        env_fns = [env_thunk(i) for i in range(num_processes)]
        return VecPyTorch(
            DummyVecEnv(env_fns, render=render)
            if synchronous or num_processes == 1
            else SubprocVecEnv(env_fns, **mp_kwargs, start_method="fork", render=render)
        )

    @classmethod
    def main(cls, cfg: DictConfig):
        return cls.run(**cls.structure_config(cfg))

    @staticmethod
    def make_env(env, seed, rank, evaluating, **kwargs):
        env = gym.make(env, **kwargs)
        env.seed(seed + rank)
        return env

    @staticmethod
    def report(frames: int, log_dir: Path, **kwargs):
        print("Frames:", frames)
        pprint(kwargs)
        try:
            wandb.log(kwargs, step=frames)
        except wandb.Error:
            pass

    @classmethod
    def run(
        cls,
        agent_args: dict,
        cuda: bool,
        cuda_deterministic: bool,
        curriculum_args: dict,
        env_args: dict,
        eval_interval: Optional[int],
        eval_steps: Optional[int],
        failure_buffer_args: dict,
        group: str,
        load_path: Path,
        log_interval: int,
        name: str,
        use_wandb: bool,
        num_frames: Optional[int],
        num_processes: int,
        ppo_args: dict,
        render: bool,
        render_eval: bool,
        rollouts_args: dict,
        seed: int,
        save_interval: int,
        train_steps: int,
    ):
        assert (eval_interval and eval_steps) or not (eval_interval or eval_steps), (
            eval_steps,
            eval_interval,
        )

        if use_wandb:
            wandb.init(group=group, name=name, project="ppo")
            os.symlink(
                os.path.abspath(".hydra/config.yaml"),
                os.path.join(wandb.run.dir, "hydra-config.yaml"),
            )
            wandb.save("hydra-config.yaml")
            log_dir = Path(wandb.run.dir)
        else:
            log_dir = Path("/tmp")
        # Properly restrict pytorch to not consume extra resources.
        #  - https://github.com/pytorch/pytorch/issues/975
        #  - https://github.com/ray-project/ray/issues/3609
        torch.set_num_threads(1)
        os.environ["OMP_NUM_THREADS"] = "1"
        save_path = Path(log_dir, CHECKPOINT_NAME)

        def run_epoch(obs, rnn_hxs, masks, envs, num_steps):
            for _ in range(num_steps):
                with torch.no_grad():
                    act = agent(
                        inputs=obs, rnn_hxs=rnn_hxs, masks=masks
                    )  # type: AgentOutputs

                action = envs.preprocess(act.action)
                # Observe reward and next obs
                obs, reward, done, infos = envs.step(action)

                # If done then clean the history of observations.
                masks = torch.tensor(
                    1 - done, dtype=torch.float32, device=obs.device
                ).unsqueeze(1)
                yield EpochOutputs(
                    obs=obs, reward=reward, done=done, infos=infos, act=act, masks=masks
                )

                rnn_hxs = act.rnn_hxs

        if render_eval and not render:
            eval_interval = 1
        if render or render_eval:
            ppo_args.update(ppo_epoch=0)
            cuda = False
        cuda &= torch.cuda.is_available()

        # reproducibility
        # if cuda_deterministic:
        #     torch.set_deterministic(True)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        np.random.seed(seed)

        if cuda:
            device = torch.device("cuda")
        else:
            device = torch.device("cpu")
        print("Using device", device)

        failure_buffer = cls.build_failure_buffer(**failure_buffer_args)
        curriculum = cls.initialize_curriculum(log_dir=log_dir, **curriculum_args)
        curriculum_setting = next(curriculum)
        train_envs = cls.make_vec_envs(
            evaluating=False,
            log_dir=log_dir,
            failure_buffer=failure_buffer,
            curriculum_setting=curriculum_setting,
            **env_args,
        )
        print("Created train_envs")
        train_envs.to(device)
        agent = cls.build_agent(envs=train_envs, **agent_args)
        rollouts = RolloutStorage(
            num_steps=train_steps,
            obs_space=train_envs.observation_space,
            action_space=train_envs.action_space,
            recurrent_hidden_state_size=agent.recurrent_hidden_state_size,
            **rollouts_args,
        )

        # copy to device
        if cuda:
            agent.to(device)
            rollouts.to(device)

        ppo = PPO(agent=agent, **ppo_args)
        train_report = EpisodeAggregator()
        train_infos = cls.build_infos_aggregator()
        train_results = {}
        if load_path:
            cls.load_checkpoint(load_path, ppo, agent, device)

        print("resetting environment...")
        rollouts.obs[0].copy_(train_envs.reset())
        print("Reset environment")
        frames_per_update = train_steps * num_processes
        frames = Counter()
        time_spent = TotalTimeKeeper()
        time_per = AverageTimeKeeper()
        time_per["iter"].tick()

        for i in itertools.count():
            frames.update(so_far=frames_per_update)
            done = num_frames is not None and frames["so_far"] >= num_frames
            if done or i == 0 or frames["since_log"] > log_interval:

                time_spent["logging"].tick()
                frames["since_log"] = 0
                report = dict(
                    **train_results,
                    **dict(train_report.items()),
                    **dict(train_infos.items()),
                    **dict(time_per.items()),
                    **dict(time_spent.items()),
                    frames=frames["so_far"],
                    log_dir=log_dir,
                )
                if failure_buffer is not None:
                    report.update({"failure buffer size": failure_buffer.qsize()})
                cls.report(**report)
                train_report.reset()
                train_infos.reset()
                time_spent["logging"].update()
                time_per["iter"].update()

                time_spent["dumping failure buffer"].tick()
                cls.dump_failure_buffer(failure_buffer, log_dir)
                time_spent["dumping failure buffer"].update()

                if eval_interval and (
                    i == 0 or done or frames["since_eval"] > eval_interval
                ):
                    print("Evaluating...")
                    time_spent["evaluating"].tick()
                    eval_report = EvalEpisodeAggregator()
                    eval_infos = EvalInfosAggregator()
                    frames["since_eval"] = 0

                    # self.envs.evaluate()
                    eval_masks = torch.zeros(num_processes, 1, device=device)
                    eval_envs = cls.make_vec_envs(
                        log_dir=log_dir,
                        failure_buffer=failure_buffer,
                        curriculum_setting=curriculum_setting,
                        evaluating=True,
                        **env_args,
                    )
                    eval_envs.to(device)
                    with agent.evaluating(eval_envs.observation_space):
                        eval_recurrent_hidden_states = torch.zeros(
                            num_processes,
                            agent.recurrent_hidden_state_size,
                            device=device,
                        )

                        for output in run_epoch(
                            obs=eval_envs.reset(),
                            rnn_hxs=eval_recurrent_hidden_states,
                            masks=eval_masks,
                            envs=eval_envs,
                            num_steps=eval_steps,
                        ):
                            eval_report.update(
                                reward=output.reward.cpu().numpy(),
                                dones=output.done,
                            )
                            eval_infos.update(*output.infos, dones=output.done)
                        cls.report(
                            **dict(eval_report.items()),
                            **dict(eval_infos.items()),
                            frames=frames["so_far"],
                            log_dir=log_dir,
                        )
                        print("Done evaluating...")
                    eval_envs.close()
                    rollouts.obs[0].copy_(train_envs.reset())
                    rollouts.masks[0] = 1
                    rollouts.recurrent_hidden_states[0] = 0
                    time_spent["evaluating"].update()
                    train_report = EpisodeAggregator()
                    train_infos = cls.build_infos_aggregator()

            if done or (save_interval and frames["since_save"] > save_interval):
                time_spent["saving"].tick()
                frames["since_save"] = 0
                cls.save_checkpoint(
                    save_path,
                    ppo=ppo,
                    agent=agent,
                    step=i,
                )
                time_spent["saving"].update()

            if done:
                break

            time_per["frame"].tick()
            time_per["update"].tick()
            for output in run_epoch(
                obs=rollouts.obs[0],
                rnn_hxs=rollouts.recurrent_hidden_states[0],
                masks=rollouts.masks[0],
                envs=train_envs,
                num_steps=train_steps,
            ):
                train_report.update(
                    reward=output.reward.cpu().numpy(),
                    dones=output.done,
                )
                train_infos.update(*output.infos, dones=output.done)
                rollouts.insert(
                    obs=output.obs,
                    recurrent_hidden_states=output.act.rnn_hxs,
                    actions=output.act.action,
                    action_log_probs=output.act.action_log_probs,
                    values=output.act.value,
                    rewards=output.reward,
                    masks=output.masks,
                )
                frames.update(
                    since_save=num_processes,
                    since_log=num_processes,
                    since_eval=num_processes,
                )
                time_per["frame"].update()

            curriculum.send((train_envs, train_infos))

            with torch.no_grad():
                next_value = agent.get_value(
                    rollouts.obs[-1],
                    rollouts.recurrent_hidden_states[-1],
                    rollouts.masks[-1],
                )

            rollouts.compute_returns(next_value.detach())
            train_results = ppo.update(rollouts)
            rollouts.after_update()
            time_per["update"].update()

    @staticmethod
    def save_checkpoint(save_path: Path, ppo: PPO, agent: Agent, step: int):
        modules = dict(
            optimizer=ppo.optimizer, agent=agent
        )  # type: Dict[str, torch.nn.Module]
        state_dict = {name: module.state_dict() for name, module in modules.items()}
        torch.save(dict(step=step, **state_dict), save_path)
        print(f"Saved parameters to {save_path}")

    @classmethod
    def structure_config(cls, cfg: DictConfig) -> Dict[str, any]:
        cfg = DictConfig(dict(flatten(cfg)))

        if cfg.render:
            cfg.num_processes = 1

        def parameters(*ms):
            for method in ms:
                yield from inspect.signature(method).parameters

        args_to_methods = cls.args_to_methods()
        args = {k: {} for k in args_to_methods}
        for k, v in cfg.items():
            if k in ("_wandb", "wandb_version", "eval_perform"):
                continue
            assigned = False
            for arg_name, methods in args_to_methods.items():
                if k in parameters(*methods):
                    args[arg_name][k] = v
                    assigned = True
            assert assigned, k
        run_args = args.pop("run_args")
        args.update(**run_args)
        return args


@hydra.main(config_name="config")
def app(cfg: DictConfig) -> None:
    Trainer.main(cfg)


if __name__ == "__main__":
    cs = ConfigStore.instance()
    cs.store(name="config", node=Config)
    app()
