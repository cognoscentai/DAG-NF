import torch
import torch.nn as nn
from .Conditionners import Conditioner, DAGConditioner
from .Normalizers import Normalizer


class NormalizingFlow(nn.Module):
    def __init__(self):
        super(NormalizingFlow, self).__init__()

    '''
    Should return the x transformed and the log determinant of the Jacobian of the transformation
    '''
    def forward(self, x, context=None):
        pass

    '''
    Should return a term relative to the loss.
    '''
    def constraintsLoss(self):
        pass

    '''
    Should return the dagness of the associated graph.
    '''
    def DAGness(self):
        pass

    '''
    Step in the optimization procedure;
    '''
    def step(self, epoch_number, loss_avg):
        pass

    '''
    Return a list containing the conditioners.
    '''
    def getConditioners(self):
        pass

    '''
        Return a list containing the normalizers.
        '''

    def getNormalizers(self):
        pass


class NormalizingFlowStep(NormalizingFlow):
    def __init__(self, conditioner: Conditioner, normalizer: Normalizer):
        super(NormalizingFlowStep, self).__init__()
        self.conditioner = conditioner
        self.normalizer = normalizer

    def forward(self, x, context=None):
        h = self.conditioner(x, context)
        z, jac = self.normalizer(x, h, context)
        return z, torch.log(jac).sum(1)

    def constraintsLoss(self):
        if type(self.conditioner) is DAGConditioner:
            return self.conditioner.loss()
        return 0.

    def DAGness(self):
        if type(self.conditioner) is DAGConditioner:
            return [self.conditioner.get_power_trace()]
        return [0.]

    def step(self, epoch_number, loss_avg):
        if type(self.conditioner) is DAGConditioner:
            self.conditioner.step(epoch_number, loss_avg)

    def getConditioners(self):
        return [self.conditioner]

    def getNormalizers(self):
        return [self.normalizer]


class FCNormalizingFlow(NormalizingFlow):
    def __init__(self, steps, z_log_density):
        super(FCNormalizingFlow, self).__init__()
        self.steps = nn.ModuleList()
        self.z_log_density = z_log_density
        for step in steps:
            self.steps.append(step)

    def forward(self, x, context=None):
        jac_tot = 0.
        inv_idx = torch.arange(x.shape[1] - 1, -1, -1).long()
        for step in self.steps:
            z, jac = step(x, context)
            x = z[:, inv_idx]
            jac_tot += jac

        return z, jac_tot

    def constraintsLoss(self):
        loss = 0.
        for step in self.steps:
                loss += step.constraintsLoss()
        return loss

    def DAGness(self):
        dagness = []
        for step in self.steps:
            dagness += step.DAGness()
        return dagness

    def step(self, epoch_number, loss_avg):
        for step in self.steps:
            step.step(epoch_number, loss_avg)

    def loss(self, z, jac):
        log_p_x = jac + self.z_log_density(z)
        return self.constraintsLoss() - log_p_x.mean()

    def getNormalizers(self):
        normalizers = []
        for step in self.steps:
            normalizers += step.getNormalizers()
        return normalizers

    def getConditioners(self):
        conditioners = []
        for step in self.steps:
            conditioners += step.getConditioners()
        return conditioners


class CNNormalizingFlow(FCNormalizingFlow):
    def __init__(self, steps, z_log_density, dropping_factors):
        super(CNNormalizingFlow, self).__init__(steps, z_log_density)
        self.dropping_factors = dropping_factors

    def forward(self, x, context=None):
        b_size = x.shape[0]
        jac_tot = 0.
        z_all = []
        for step, drop_factors in zip(self.steps, self.dropping_factors):
            z, jac = step(x, context)
            d_c, d_h, d_w = drop_factors
            C, H, W = step.img_sizes
            c, h, w = int(C/d_c), int(H/d_h), int(W/d_w)
            z_reshaped = z.view(-1, C, H, W).unfold(1, d_c, d_c).unfold(2, d_h, d_h) \
                    .unfold(3, d_w, d_w).contiguous().view(b_size, c, h, w, -1)
            z_all += [z_reshaped[:, :, :, 1:].contiguous().view(b_size, -1)]
            x = z.view(-1, C, H, W).unfold(1, d_c, d_c).unfold(2, d_h, d_h) \
                    .unfold(3, d_w, d_w).contiguous().view(b_size, c, h, w, -1)[:, :, :, :, 0] \
                .contiguous().view(b_size, -1)
            jac_tot += jac
        z = torch.cat(z_all, 1)
        return z, jac_tot