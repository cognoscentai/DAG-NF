from models import DAGNF, MLP, MNISTCNN
import torch
from timeit import default_timer as timer
import lib.utils as utils
import os
import matplotlib
import matplotlib.pyplot as plt
import networkx as nx
from torchvision import datasets, transforms
from lib.transform import AddUniformNoise, ToTensor, HorizontalFlip, Transpose, Resize
import numpy as np
import math
import torch.nn as nn
from UMNN import UMNNMAFFlow
from models.NormalizingFlowFactories import buildMNISTNormalizingFlow, buildCIFAR10NormalizingFlow
from models.Normalizers import AffineNormalizer, MonotonicNormalizer
import torchvision.datasets as dset
import torchvision.transforms as tforms


def add_noise(x):
    """
    [0, 1] -> [0, 255] -> add noise -> [0, 1]
    """
    noise = x.new().resize_as_(x).uniform_()
    x = x * 255 + noise
    x = x / 256
    return x


def compute_bpp(ll, x, alpha=1e-6):
    d = x.shape[1]
    bpp = -ll / (d * np.log(2)) - np.log2(1 - 2 * alpha) + 8 \
          + 1 / d * (torch.log2(torch.sigmoid(x)) + torch.log2(1. - torch.sigmoid(x))).sum(1)
    return bpp


def load_data(dataset="MNIST", batch_size=100, cuda=-1):
    if dataset == "MNIST":
        data = datasets.MNIST('./MNIST', train=True, download=True,
                              transform=transforms.Compose([
                                  AddUniformNoise(),
                                  ToTensor()
                              ]))

        train_data, valid_data = torch.utils.data.random_split(data, [50000, 10000])

        test_data = datasets.MNIST('./MNIST', train=False, download=True,
                                   transform=transforms.Compose([
                                       AddUniformNoise(),
                                       ToTensor()
                                   ]))
        kwargs = {'num_workers': 0, 'pin_memory': True} if cuda > -1 else {}

        train_loader = torch.utils.data.DataLoader(train_data, batch_size=batch_size, shuffle=True, drop_last=False, **kwargs)
        valid_loader = torch.utils.data.DataLoader(valid_data, batch_size=batch_size, shuffle=True, drop_last=False, **kwargs)
        test_loader = torch.utils.data.DataLoader(test_data, batch_size=batch_size, shuffle=True, drop_last=False, **kwargs)
    elif dataset == "CIFAR10":
        im_dim = 3
        im_size = 32  # if args.imagesize is None else args.imagesize
        trans = lambda im_size: tforms.Compose([tforms.Resize(im_size), tforms.ToTensor(), add_noise])
        train_data = dset.CIFAR10(
            root="./data", train=True, transform=tforms.Compose([
                tforms.Resize(im_size),
                tforms.RandomHorizontalFlip(),
                tforms.ToTensor(),
                add_noise,
            ]), download=True
        )
        test_data = dset.CIFAR10(root="./data", train=False, transform=trans(im_size), download=True)
        kwargs = {'num_workers': 0, 'pin_memory': True} if cuda > -1 else {}

        train_loader = torch.utils.data.DataLoader(train_data, batch_size=batch_size, drop_last=False, shuffle=True, **kwargs)
        # WARNING VALID = TEST
        valid_loader = torch.utils.data.DataLoader(test_data, batch_size=batch_size, drop_last=False, shuffle=True, **kwargs)
        test_loader = torch.utils.data.DataLoader(test_data, batch_size=batch_size, drop_last=False, shuffle=True, **kwargs)
    return train_loader, valid_loader, test_loader


def train(dataset="MNIST", load=True, nb_step_dual=100, nb_steps=20, path="", l1=.1, nb_epoch=10000, b_size=100,
          int_net=[50, 50, 50], all_args=None, file_number=None, train=True, solver="CC", weight_decay=1e-5,
          learning_rate=1e-3, batch_per_optim_step=1, n_gpu=1, norm_type='Affine'):
    logger = utils.get_logger(logpath=os.path.join(path, 'logs'), filepath=os.path.abspath(__file__))
    logger.info(str(all_args))


    if load:
        train = False
        file_number = "_" + file_number if file_number is not None else ""

    batch_size = b_size
    best_valid_loss = np.inf

    logger.info("Loading data...")
    train_loader, valid_loader, test_loader = load_data(dataset, batch_size)
    alpha = 1e-6 if dataset == "MNIST" else .05

    logger.info("Data loaded.")

    master_device = "cuda:0" if torch.cuda.is_available() else "cpu"

    # -----------------------  Model Definition ------------------- #
    logger.info("Creating model...")
    if norm_type == 'Affine':
        normalizer_type = AffineNormalizer
        normalizer_args = {}
    else:
        normalizer_type = MonotonicNormalizer
        normalizer_args = {"integrand_net": int_net, "cond_size": 30, "nb_steps": 15, "solver": solver}

    if dataset == "MNIST":
        inner_model = buildMNISTNormalizingFlow([2, 2, 2], normalizer_type, normalizer_args, l1)
    elif dataset == "CIFAR10":
        inner_model = buildCIFAR10NormalizingFlow([2, 2, 2, 2], normalizer_type, normalizer_args, l1)
    else:
        logger.info("Wrong dataset name. Training aborted.")
        exit()
    model = nn.DataParallel(inner_model, device_ids=list(range(n_gpu))).to(master_device)


    opt = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=weight_decay)

    if load:
        logger.info("Loading model...")
        model.load_state_dict(torch.load(path + '/model%s.pt' % file_number, map_location={"cuda:0": master_device}))
        model.train()
        opt.load_state_dict(torch.load(path + '/ADAM%s.pt' % file_number, map_location={"cuda:0": master_device}))
    logger.info("...Model built.")
    logger.info("Training starts:")

    # ----------------------- Main Loop ------------------------- #
    for epoch in range(nb_epoch):
        ll_tot = 0
        start = timer()
        if train:
            model.to(master_device)
            # ----------------------- Training Loop ------------------------- #
            for batch_idx, (cur_x, target) in enumerate(train_loader):
                cur_x = cur_x.view(batch_size, -1).float().to(master_device)
                for normalizer in model.module.getNormalizers():
                    if type(normalizer) is MonotonicNormalizer:
                        normalizer.nb_steps = nb_steps + torch.randint(0, 10, [1])[0].item()
                z, jac = model(cur_x)
                loss = model.module.loss(z, jac)/(batch_per_optim_step * n_gpu)
                if math.isnan(loss.item()):
                    print("Error Nan in loss")
                    print("Dagness:", model.module.DAGness())
                    exit()
                ll_tot += loss.detach()
                if batch_idx % batch_per_optim_step == 0:
                    opt.zero_grad()

                loss.backward(retain_graph=True)
                if (batch_idx + 1) % batch_per_optim_step == 0:
                    opt.step()

            with torch.no_grad():
                print("Dagness:", model.module.DAGness())

            ll_tot /= (batch_idx + 1)
            model.module.step(epoch, ll_tot)

        else:
            ll_tot = 0.

        # ----------------------- Valid Loop ------------------------- #
        ll_test = 0.
        bpp_test = 0.
        model.to(master_device)
        with torch.no_grad():
            for normalizer in model.module.getNormalizers():
                if type(normalizer) is MonotonicNormalizer:
                    normalizer.nb_steps = 30
            for batch_idx, (cur_x, target) in enumerate(valid_loader):
                cur_x = cur_x.view(batch_size, -1).float().to(master_device)
                z, jac = model(cur_x)
                ll = (model.module.z_log_density(z) + jac)
                ll_test += ll.mean().item()
                bpp_test += compute_bpp(ll, cur_x.view(batch_size, -1).float().to(master_device), alpha).mean().item()
        ll_test /= batch_idx + 1
        bpp_test /= batch_idx + 1
        end = timer()

        dagness = max(model.module.DAGness())
        logger.info(
            "epoch: {:d} - Train loss: {:4f} - Valid log-likelihood: {:4f} - Valid BPP {:4f} - <<DAGness>>: {:4f} "
            "- Elapsed time per epoch {:4f} (seconds)".format(epoch, ll_tot, ll_test, bpp_test, dagness, end - start))

        if epoch % 10 == 0:
            stoch_gate, noise_gate, s_thresh = [], [], []
            with torch.no_grad():
                for conditioner in model.module.getConditioners():
                    stoch_gate.append(conditioner.stoch_gate)
                    noise_gate.append(conditioner.noise_gate)
                    s_thresh.append(conditioner.s_thresh)
                    conditioner.stoch_gate = False
                    conditioner.noise_gate = False
                    conditioner.s_thresh = True
                for threshold in [.95, .5, .1, .01, .0001]:
                    for conditioner in model.getConditioners():
                        conditioner.h_thresh = threshold
                    # Valid loop
                    ll_test = 0.
                    bpp_test = 0.
                    for batch_idx, (cur_x, target) in enumerate(valid_loader):
                        cur_x = cur_x.view(batch_size, -1).float().to(master_device)
                        z, jac = model(cur_x)
                        ll = (model.module.z_log_density(z) + jac)
                        ll_test += ll.mean().item()
                        bpp_test += compute_bpp(ll, cur_x.view(batch_size, -1).float().to(master_device), alpha).mean().item()
                    ll_test /= batch_idx + 1
                    bpp_test /= batch_idx + 1
                    dagness = max(model.module.DAGness())
                    logger.info("epoch: {:d} - Threshold: {:4f} - Valid log-likelihood: {:4f} - Valid BPP {:4f} - <<DAGness>>: {:4f}".
                        format(epoch, threshold, ll_test, bpp_test, dagness))

                if dagness < 1e-5 and -ll_test < best_valid_loss:
                    logger.info("------- New best validation loss with threshold %f --------" % threshold)
                    torch.save(model.state_dict(), path + '/best_model.pt' % epoch)
                    best_valid_loss = -ll_test
                    # Valid loop
                    ll_test = 0.
                    for batch_idx, (cur_x, target) in enumerate(test_loader):
                        z, jac = model(cur_x)
                        ll = (model.z_log_density(z) + jac)
                        ll_test += ll.mean().item()
                        bpp_test += compute_bpp(ll, cur_x.view(batch_size, -1).float().to(master_device), alpha).mean().item()

                    ll_test /= batch_idx + 1
                    bpp_test /= batch_idx + 1
                    logger.info("epoch: {:d} - Threshold: {:4f} - Test log-likelihood: {:4f} - Test BPP {:4f} - <<DAGness>>: {:4f}".
                                format(epoch, threshold, ll_test, bpp_test, dagness))
                    if dataset == "MNIST":
                        A_1 = model.module.getConditioners()[0].soft_thresholded_A()[0, :].view(28, 28)
                        plt.matshow(A_1)
                        plt.colorbar()
                        plt.savefig(path + "/A_1_epoch_%d.png" % epoch)
                        A_350 = model.module.getConditioners()[0].soft_thresholded_A()[350, :].view(28, 28)
                        plt.matshow(A_350)
                        plt.colorbar()
                        plt.savefig(path + "/A_350_epoch_%d.png" % epoch)
                    elif dataset == "CIFAR10":
                        A_1 = model.module.getConditioners()[0].soft_thresholded_A()[0, :].view(3, 32, 32)
                        plt.subplot(1, 3, 1)
                        plt.matshow(A_1[0, :, :])
                        plt.subplot(1, 3, 2)
                        plt.matshow(A_1[1, :, :])
                        plt.subplot(1, 3, 3)
                        plt.matshow(A_1[2, :, :])
                        plt.colorbar()
                        plt.savefig(path + "/A_1_epoch_%d.png" % epoch)
                        A_1500 = model.module.getConditioners()[0].soft_thresholded_A()[1500, :].view(3, 32, 32)
                        plt.subplot(1, 3, 1)
                        plt.matshow(A_1500[0, :, :])
                        plt.subplot(1, 3, 2)
                        plt.matshow(A_1500[1, :, :])
                        plt.subplot(1, 3, 3)
                        plt.matshow(A_1500[2, :, :])
                        plt.colorbar()
                        plt.savefig(path + "/A_1500_epoch_%d.png" % epoch)
                for i, conditioner in enumerate(model.module.getConditioners()):
                    conditioner.h_thresh = 0.
                    conditioner.stoch_gate = stoch_gate[i]
                    conditioner.noise_gate = noise_gate[i]
                    conditioner.s_thresh = s_thresh[i]

        if epoch % nb_step_dual == 0:
            logger.info("Saving model N°%d" % epoch)
            torch.save(model.state_dict(), path + '/model_%d.pt' % epoch)
            torch.save(opt.state_dict(), path + '/ADAM_%d.pt' % epoch)

        torch.save(model.state_dict(), path + '/model.pt')
        torch.save(opt.state_dict(), path + '/ADAM.pt')

import argparse

parser = argparse.ArgumentParser(description='')
parser.add_argument("-load", default=False, action="store_true", help="Load a model ?")
parser.add_argument("-folder", default="", help="Folder")
parser.add_argument("-nb_steps_dual", default=100, type=int,
                    help="number of step between updating Acyclicity constraint and sparsity constraint")
parser.add_argument("-l1", default=10., type=float, help="Maximum weight for l1 regularization")
parser.add_argument("-nb_epoch", default=10000, type=int, help="Number of epochs")
parser.add_argument("-b_size", default=1, type=int, help="Batch size")
parser.add_argument("-int_net", default=[50, 50, 50], nargs="+", type=int, help="NN hidden layers of UMNN")
parser.add_argument("-nb_steps", default=20, type=int, help="Number of integration steps.")
parser.add_argument("-f_number", default=None, type=str, help="Number of heating steps.")
parser.add_argument("-solver", default="CC", type=str, help="Which integral solver to use.",
                    choices=["CC", "CCParallel"])
parser.add_argument("-nb_flow", type=int, default=1, help="Number of steps in the flow.")
parser.add_argument("-test", default=False, action="store_true")
parser.add_argument("-weight_decay", default=1e-5, type=float, help="Weight decay value")
parser.add_argument("-learning_rate", default=1e-3, type=float, help="Weight decay value")
parser.add_argument("-batch_per_optim_step", default=1, type=int, help="Number of batch to accumulate")
parser.add_argument("-nb_gpus", default=1, type=int, help="Number of gpus to train on")
parser.add_argument("-dataset", default="MNIST", type=str, choices=["MNIST", "CIFAR10"])
parser.add_argument("-normalizer", default="Affine", type=str, choices=["Affine", "Monotonic"])

args = parser.parse_args()
from datetime import datetime
now = datetime.now()

path = args.dataset + "/" + now.strftime("%m_%d_%Y_%H_%M_%S") if args.folder == "" else args.folder
if not (os.path.isdir(path)):
    os.makedirs(path)
train(dataset=args.dataset, load=args.load, path=path, nb_step_dual=args.nb_steps_dual, l1=args.l1, nb_epoch=args.nb_epoch,
      int_net=args.int_net, b_size=args.b_size, all_args=args,
      nb_steps=args.nb_steps, file_number=args.f_number, norm_type=args.normalizer,
      solver=args.solver, train=not args.test, weight_decay=args.weight_decay, learning_rate=args.learning_rate,
      batch_per_optim_step=args.batch_per_optim_step, n_gpu=args.nb_gpus)
