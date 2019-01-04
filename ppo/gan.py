import torch
import torch.nn as nn

from ppo.utils import mlp
from utils import space_to_size

from ppo.utils import mlp


class GAN(nn.Module):
    def __init__(self, goal_space: Box, hidden_size, **kwargs):
        super().__init__()
        self.goal_space = goal_space
        goal_size = space_to_size(goal_space)
        self.hidden_size = hidden_size
        self.network = nn.Sequential(
            mlp(num_inputs=hidden_size,
                hidden_size=hidden_size,
                num_outputs=2 * goal_size,
                name='gan',
                **kwargs), torch.nn.Softplus())
        self.regularizer = None

    def goal_input(self, num_outputs):
        return torch.ones((num_outputs, self.hidden_size))

    def forward(self, *inputs):
        raise NotImplementedError

    def dist(self, num_inputs):
        network_out = self.network(self.goal_input(num_inputs))
        params = torch.chunk(network_out, 2, dim=-1)
        return torch.distributions.Beta(*params)

    def log_prob(self, goal):
        num_inputs = goal.size()[0]
        return self.dist(num_inputs).log_prob(goal)

    def sample(self, num_outputs):
        dist = self.dist(num_outputs)
        sample = dist.sample()
        high = torch.tensor(self.goal_space.high)
        low = torch.tensor(self.goal_space.low)
        squashed = sample * (high - low) + low
        mean_log_prob = dist.log_prob(dist.mean).mean(dim=0)
        if self.regularizer is None:
            self.regularizer = mean_log_prob
        else:
            self.regularizer += .01 * (mean_log_prob - self.regularizer)
        importance_weighting = (self.regularizer - dist.log_prob(sample)).sum(
            dim=-1).exp()
        return squashed, importance_weighting.view(-1, 1)

    def parameters(self):
        return self.network.parameters()

    def to(self, device):
        self.network.to(device)
