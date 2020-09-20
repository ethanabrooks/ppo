from typing import List, Generator, Tuple, Optional

from gym import spaces
import numpy as np

from lines import If, While
from utils import hierarchical_parse_args, RESET, GREEN, RED

import env
import keyboard_control
from env import ObjectMap, Coord, Line, State, Action, Obs


class Env(env.Env):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # noinspection PyProtectedMember
        self.observation_space = spaces.Dict(
            Obs(
                active=spaces.Discrete(self.n_lines + 1),
                inventory=spaces.MultiBinary(0),
                lines=spaces.MultiDiscrete(
                    np.array([len(self.possible_lines)] * self.n_lines)
                ),
                obs=spaces.Discrete(2),
            )._asdict()
        )

    def state_generator(
        self, objects: ObjectMap, agent_pos: Coord, lines: List[Line], **kwargs
    ) -> Generator[State, Tuple[int, int], None]:

        line_iterator = self.line_generator(lines)
        condition_bit = self.random.choice(2)
        subtask_iterator = self.subtask_generator(
            line_iterator, lines, condition_bit=condition_bit
        )
        prev, ptr = 0, next(subtask_iterator)
        term = False

        while True:
            state = State(
                obs=[condition_bit],
                prev=prev,
                ptr=ptr,
                term=term,
                subtask_complete=True,
                time_remaining=0,
                counts=condition_bit,
                inventory=None,
            )
            subtask_id, lower_level_index = yield state
            term = subtask_id != self.subtasks.index(lines[ptr].id)
            condition_bit = self.random.choice(2)
            prev, ptr = ptr, subtask_iterator.send(dict(condition_bit=condition_bit))

    def evaluate_line(self, *args, condition_bit, **kwargs) -> bool:
        return bool(condition_bit)

    def populate_world(self, lines) -> Optional[Tuple[Coord, ObjectMap]]:
        return (0, 0), {}

    def feasible(self, objects, lines) -> bool:
        return True

    def render_world(
        self,
        state,
        action,
        lower_level_action,
        reward,
        cumulative_reward,
    ):
        if action is not None and action < len(self.subtasks):
            print("Selected:", self.subtasks[action], action)
        print("Action:", action)
        print("Reward", reward)
        print("Cumulative", cumulative_reward)
        for i, subtask in enumerate(self.subtasks):
            print(i, subtask)

    def render_instruction(
        self,
        term,
        success,
        lines,
        state,
        agent_ptr,
    ):

        if term:
            print(GREEN if success else RED)
        indent = 0
        for i, line in enumerate(lines):
            if i == state.ptr and i == agent_ptr:
                pre = "+ "
            elif i == agent_ptr:
                pre = "- "
            elif i == state.ptr:
                pre = "| "
            else:
                pre = "  "
            indent += line.depth_change[0]
            if type(line) in (If, While):
                evaluation = state.counts
                line_str = f"{line} {evaluation}"
            else:
                line_str = str(line)
            print("{:2}{}{}{}".format(i, pre, " " * indent, line_str))
            indent += line.depth_change[1]
        print("Condition bit:", state.counts)
        print(RESET)

    def get_observation(self, obs, preprocessed_lines, state):
        return Obs(
            obs=obs,
            lines=preprocessed_lines,
            active=self.n_lines if state.ptr is None else state.ptr,
            inventory=np.array([]),
        )


def main(env: Env):
    def action_fn(string):
        try:
            action = int(string)
            if action > env.num_subtasks:
                raise ValueError
        except ValueError:
            return None

        return np.array(Action(upper=action, lower=0, delta=0, dg=0, ptr=0))

    keyboard_control.run(env, action_fn=action_fn)


if __name__ == "__main__":
    import argparse

    PARSER = argparse.ArgumentParser()
    PARSER.add_argument("--min-eval-lines", type=int, required=True)
    PARSER.add_argument("--max-eval-lines", type=int, required=True)
    env.build_parser(PARSER)
    PARSER.add_argument("--seed", default=0, type=int)
    main(Env(rank=0, lower_level=None, **hierarchical_parse_args(PARSER)))
