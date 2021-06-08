import torch.nn as nn
import torch.nn.functional as F
from torch.optim import SGD
import torch as t
from scipy import constants
import numpy as np
import pandas as pd
from pyhdx.models import Protein


class DeltaGFit(nn.Module):
    def __init__(self, deltaG):
        super(DeltaGFit, self).__init__()
        self.deltaG = deltaG

    def forward(self, temperature, X, k_int, timepoints):
        """
        # inputs, list of:
            temperatures: scalar (1,)
            X (N_peptides, N_residues)
            k_int: (N_peptides, 1)

        """

        pfact = t.exp(self.deltaG / (constants.R * temperature))
        uptake = 1 - t.exp(-t.matmul((k_int / (1 + pfact)), timepoints))
        return t.matmul(X, uptake)


def estimate_errors(hdxm, deltaG):
    """
    Calculate covariances

    Parameters
    ----------
    hdxm: HDXMeasurement
    deltaG: numpy array
        Array with deltaG values.

    Returns
    -------

    """
    # boolean array to select residues which are exchanging (ie no nterminal resiudes, no prolines, no regions without coverage)
    bools = hdxm.coverage['exchanges'].to_numpy()
    r_number = hdxm.coverage.r_number[bools]  # Residue number which exchange
    deltaG = t.tensor(deltaG[bools], dtype=t.float64)

    tensors = hdxm.get_tensors(exchanges=True)

    def calc_loss(deltaG_input):
        criterion = t.nn.MSELoss(reduction='sum')
        pfact = t.exp(deltaG_input.unsqueeze(-1) / (constants.R * tensors['temperature']))
        uptake = 1 - t.exp(-t.matmul((tensors['k_int'] / (1 + pfact)), tensors['timepoints']))
        output = t.matmul(tensors['X'], uptake)

        loss = criterion(output, tensors['uptake'])
        return loss

    hessian = t.autograd.functional.hessian(calc_loss, deltaG)
    hessian_inverse = t.inverse(-hessian)
    covariance = np.sqrt(np.abs(np.diagonal(hessian_inverse)))

    #todo return pd series?
    return Protein({'covariance': covariance, 'r_number': r_number}, index='r_number')


class TorchFitResult(object):
    """
    PyTorch Fit result object.

    Parameters
    ----------

    data_obj: HDXMeasurement or HDXMeasurementSet
    model
    **metdata

    """
    def __init__(self, data_obj, model, **metadata):
        self.data_obj = data_obj
        self.model = model
        self.metadata = metadata

    @property
    def mse_loss(self):
        """obj:`float`: Losses from mean squared error part of Lagrangian"""
        mse_loss = self.metadata['mse_loss'][-1]
        return mse_loss

    @property
    def total_loss(self):
        """obj:`float`: Total loss value of the Lagrangian"""
        total_loss = self.metadata['total_loss'][-1]
        return total_loss

    @property
    def reg_loss(self):
        """obj:`float`: Losses from regularization part of Lagrangian"""
        return self.total_loss - self.mse_loss

    @property
    def regularization_percentage(self):
        """obj:`float`: Percentage part of the total loss that is regularization loss"""
        return (self.reg_loss / self.total_loss) * 100

    @property
    def deltaG(self):
        return self.model.deltaG.detach().numpy().squeeze()


class TorchSingleFitResult(TorchFitResult):
    #todo perhaps pass KineticsFitting object (which includes temperature) (yes do then it can also have methods which return inputs)
    def __init__(self, *args, **kwargs):
        super(TorchSingleFitResult, self).__init__(*args, **kwargs)

    @property
    def temperature(self):
        return self.data_obj.temperature

    @property
    def output(self):
        out_dict = {}
        out_dict['r_number'] = self.data_obj.coverage.r_number
        out_dict['sequence'] = self.data_obj.coverage['sequence'].to_numpy()
        out_dict['_deltaG'] = self.deltaG
        out_dict['deltaG'] = out_dict['_deltaG'].copy()
        out_dict['deltaG'][~self.data_obj.coverage['exchanges']] = np.nan
        if self.temperature is not None:
            pfact = np.exp(out_dict['deltaG'] / (constants.R * self.temperature))
            out_dict['pfact'] = pfact

        #todo add possibility to add append series to protein?
        #todo update order of columns
        protein = Protein(out_dict, index='r_number')
        protein_cov = estimate_errors(self.data_obj, self.deltaG)
        protein = protein.join(protein_cov)
        return protein

    def __call__(self, timepoints):
        """output: Np x Nt array"""
        #todo fix and tests
        with t.no_grad():
            temperature = t.Tensor([self.temperature])
            X = t.Tensor(self.data_obj.coverage.X)  # Np x Nr
            k_int = t.Tensor(self.data_obj.coverage['k_int'].to_numpy()).unsqueeze(-1)  # Nr x 1
            timepoints = t.Tensor(timepoints).unsqueeze(0)  # 1 x Nt
            inputs = [temperature, X, k_int, timepoints]

            output = self.model(*inputs)
        return output.detach().numpy()


class TorchBatchFitResult(TorchFitResult):
    def __init__(self, *args, **kwargs):
        super(TorchBatchFitResult, self).__init__(*args, **kwargs)

    @property
    def output(self):
        #todo directly create dataframe

        quantities = ['_deltaG', 'deltaG', 'covariance', 'pfact']

        names = [hdxm.name or hdxm.state for hdxm in self.data_obj.hdxm_list]

        iterables = [names, quantities]
        col_index = pd.MultiIndex.from_product(iterables, names=['State', 'Quantity'])
        output_data = np.zeros((self.data_obj.Nr, self.data_obj.Ns * len(quantities)))

        g_values = self.deltaG
        g_values_nan = g_values.copy()
        g_values_nan[~self.data_obj.exchanges] = np.nan
        pfact = np.exp(g_values / (constants.R * self.data_obj.temperature[:, np.newaxis]))

        output_data[:, 0::len(quantities)] = g_values.T
        output_data[:, 1::len(quantities)] = g_values_nan.T

        for i, data_obj in enumerate(self.data_obj.hdxm_list):
            #todo this could use some pandas
            i0 = data_obj.coverage.interval[0] - self.data_obj.interval[0]
            i1 = data_obj.coverage.interval[1] - self.data_obj.interval[0]

            cov = estimate_errors(data_obj, g_values[i, i0:i1])  # returns a protein? should be series
            pd_series = cov['covariance']
            pd_series = pd_series.reindex(self.data_obj.r_number)

            output_data[:, 2+i*len(quantities)] = pd_series.to_numpy()

        output_data[:, 3::len(quantities)] = pfact.T

        df = pd.DataFrame(output_data, index=self.data_obj.r_number, columns=col_index)

        return Protein(df)

        # use multi index df: https://stackoverflow.com/questions/24290495/constructing-3d-pandas-dataframe