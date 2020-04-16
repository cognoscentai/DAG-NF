from Conditioner import Conditioner
import torch
import torch.nn as nn


class CouplingMLP(nn.Module):
    def __init__(self, in_size, hidden, out_size, cond_in = 0):
        super(CouplingMLP, self).__init__()
        l1 = [in_size - int(in_size/2) + cond_in] + hidden
        l2 = hidden + [out_size * int(in_size/2)]
        layers = []
        for h1, h2 in zip(l1, l2):
            layers += [nn.Linear(h1, h2), nn.ReLU()]
        layers.pop()
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        return self.net(x)


class CouplingConditioner(Conditioner):
    def __init__(self, in_size, embedding_net, out_size):
        super(CouplingConditioner, self).__init__()
        self.in_size = in_size
        self.out_size = out_size
        self.cond_size = int(in_size/2)
        self.indep_size = in_size - self.cond_size
        self.embeding_net = embedding_net
        self.constants = nn.Parameter(torch.randn(self.indep_size, out_size))

    def forward(self, x, context=None):
        if context is not None:
            x = torch.cat((x, context), 1)
        h1 = self.constants.unsqueeze(0).expand(x.shape[0], -1, -1)
        h2 = self.embeding_net(x[:, :self.indep_size]).view(x.shape[0], self.cond_size, self.out_size)
        return torch.cat((h1, h2), 1)
