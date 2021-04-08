from pyhdx.models import PeptideMasterTable, KineticsSeries, Protein, array_intersection
from pyhdx.panel.widgets import NumericInput
from pyhdx.panel.data_sources import DataSource, MultiIndexDataSource, DataFrameSource
from pyhdx.panel.base import ControlPanel, DEFAULT_COLORS, DEFAULT_CLASS_COLORS
from pyhdx.fitting import KineticsFitting
from pyhdx.fileIO import read_dynamx, txt_to_np, fmt_export, csv_to_protein, txt_to_protein, csv_to_dataframe
from pyhdx.support import autowrap, colors_to_pymol, rgb_to_hex, hex_to_rgb, hex_to_rgba
from pyhdx import VERSION_STRING
from scipy import constants
import param
import panel as pn
import numpy as np
from numpy.lib.recfunctions import append_fields
from pathlib import Path
from skimage.filters import threshold_multiotsu
from numpy.lib.recfunctions import stack_arrays
from io import StringIO
from tornado.ioloop import IOLoop
from functools import partial
from bokeh.models import ColumnDataSource, LinearColorMapper, ColorBar
from bokeh.plotting import figure
from collections import namedtuple
import operator
import matplotlib as mpl
import matplotlib.pyplot as plt
import itertools
import pandas as pd

from .widgets import ColoredStaticText, ASyncProgressBar

HalfLifeFitResult = namedtuple('HalfLifeFitResult', ['output'])


class MappingFileInputControl(ControlPanel):
    """
    This controller allows users to upload *.txt files where quantities (protection factors, Gibbs free energy, etc) are
    mapped to a linear sequence. The data is then used further downstream to generate binary comparisons between datasets.

    The column should be tab separated with on the last header line (starts with '#') the names of the columns. Columns
    should be tab-delimited.
    """
    header = 'File Input'

    input_file = param.Parameter(default=None, doc='Input file to add to available datasets')
    dataset_name = param.String(doc='Name for the dataset to add. Defaults to filename')
    offset = param.Integer(default=0, doc="Offset to add to the file's r_number column")
    add_dataset = param.Action(lambda self: self._action_add_dataset(),
                               doc='Add the dataset to available datasets')
    datasets_list = param.ListSelector(doc='Current datasets', label='Datasets')
    remove_dataset = param.Action(lambda self: self._action_remove_dataset(),
                                  doc='Remove selected datasets')

    def __init__(self, parent, **params):
        super(MappingFileInputControl, self).__init__(parent, **params)
        self.parent.param.watch(self._datasets_updated, ['datasets'])

    def make_dict(self):
        return self.generate_widgets(input_file=pn.widgets.FileInput)

    @param.depends('input_file', watch=True)
    def _input_file_updated(self):
        self.dataset_name = self.dataset_name or Path(self.widget_dict['input_file'].filename).stem

    @property
    def protein(self):
        """The protein object from the currently selected file in the file widget"""

        try:
            sio = StringIO(self.input_file.decode())
        except UnicodeDecodeError:
            self.parent.logger.info('Invalid file type, supplied file is not a text file')
            return None
        try:
            sio.seek(0)
            protein = txt_to_protein(sio)
        except KeyError:
            sio.seek(0)
            protein = csv_to_protein(sio)
        return protein

    def _add_dataset(self):
        self.parent.datasets[self.dataset_name] = self.protein

    #todo refactor dataset to protein_something
    def _action_add_dataset(self):
        if self.dataset_name in self.parent.datasets.keys():
            self.parent.logger.info(f'Dataset {self.dataset_name} already added')
        elif not self.dataset_name:
            self.parent.logger.info('The added comparison needs to have a name')
        elif not self.input_file:
            self.parent.logger.info('Empty or no file selected')
        elif self.protein is not None:
            self._add_dataset()
            self.parent.param.trigger('datasets')

        self.widget_dict['input_file'].filename = ''
        self.widget_dict['input_file'].value = b''

        self.dataset_name = ''

    def _action_remove_dataset(self):
        if self.datasets_list is not None:
            for dataset_name in self.datasets_list:
                self.parent.datasets.pop(dataset_name)
            self.parent.param.trigger('datasets')

    def _datasets_updated(self, events):
        self.param['datasets_list'].objects = list(self.parent.datasets.keys())


import itertools
cmap_cycle = itertools.cycle(['gray','PiYG', 'jet'])

class CSVFileInputControl(ControlPanel):
    input_file = param.Parameter()
    load_file = param.Action(lambda self: self._action_load())
    temp_new_data = param.Action(lambda self: self._action_new_data())
    temp_new_cmap = param.Action(lambda self: self._action_new_cmap())

    temp_update_filter = param.Action(lambda self: self._action_exposure())
    temp_cmap_rect = param.Action(lambda self: self._action_cmap_rect())

    #cmap_obj = param.ObjectSelector(default='viridis', objects=['viridis', 'plasma', 'magma'])


    def make_dict(self):
        return self.generate_widgets(input_file=pn.widgets.FileInput(accept='.csv,.txt'))

    def _action_load(self):
        sio = StringIO(self.input_file.decode('UTF-8'))
        df = csv_to_dataframe(sio)
        source = DataFrameSource(df=df)

    def _action_new_data(self):

        source = self.parent.sources['torch_fit']
        table = source.get('torch_fit')

        size = len(table)

        new_data = 40e3*np.random.rand(size)

        table['deltaG'] = new_data
        print('Source data updated')

        self.parent.update()

    def _action_new_cmap(self):
        cmap_name = np.random.choice(['viridis', 'inferno', 'plasma'])
        cmap = mpl.cm.get_cmap(cmap_name)

        transform = self.parent.transforms['cmap']
        transform.cmap = cmap

        print('cmap updated')
        self.parent.update()

    def _action_exposure(self):
        print('here we go')
        filter = self.parent.filters['exposure']
        filter.widget.value = 0.

        self.parent.update()

    def _action_cmap_rect(self):
        new_cmap = next(cmap_cycle)

        rect_view = self.parent.figure_panels['rect_plot']
        rect_view.opts['cmap'] = new_cmap

        self.parent.update()

        item = self.parent.rows['rect_plot'][0]
        print('item', item)
        #item.param.trigger('object')


class TestFileInputControl(ControlPanel):
    input_file = param.Parameter()
    load_file = param.Action(lambda self: self._action_load())


    _layout = {
        'self': None,
        'filters.exposure_slider': None
    }

    def __init__(self, parent, **params):
        super().__init__(parent, **params)
        self._layout = {
            'self': None,
            'filters.exposure_slider': None
        }

        self.update_box()
        print('layout in init', self._layout)

    def make_dict(self):
        return self.generate_widgets(input_file=pn.widgets.FileInput(accept='.csv,.txt'))

    def _action_load(self):
        sio = StringIO(self.input_file.decode('UTF-8'))
        df = csv_to_dataframe(sio)
        source = DataFrameSource(df=df)


class PeptideFileInputControl(ControlPanel):
    """
    This controller allows users to input .csv file (Currently only DynamX format) of 'state' peptide uptake data.
    Users can then choose how to correct for back-exchange and which 'state' and exposure times should be used for
    analysis.

    """
    header = 'Peptide Input'

    input_files = param.List()

    be_mode = param.Selector(doc='Select method of back exchange correction', label='Back exchange correction method', objects=['FD Sample', 'Flat percentage'])
    fd_state = param.Selector(doc='State used to normalize uptake', label='FD State')
    fd_exposure = param.Selector(doc='Exposure used to normalize uptake', label='FD Exposure')
    exp_state = param.Selector(doc='State for selected experiment', label='Experiment State')
    exp_exposures = param.ListSelector(default=[], objects=[''], label='Experiment Exposures'
                                       , doc='Selected exposure time to use')

    be_percent = param.Number(28., bounds=(0, 100), doc='Global percentage of back-exchange',
                              label='Back exchange percentage')

    drop_first = param.Integer(1, bounds=(0, None), doc='Select the number of N-terminal residues to ignore.')
    ignore_prolines = param.Boolean(True, constant=True, doc='Prolines are ignored as they do not exchange D.')
    d_percentage = param.Number(95., bounds=(0, 100), doc='Percentage of deuterium in the labelling buffer',
                                label='Deuterium percentage')
    fd_percentage = param.Number(95., bounds=(0, 100), doc='Percentage of deuterium in the FD control sample buffer',
                                 label='FD Deuterium percentage')
    temperature = param.Number(293.15, bounds=(0, 373.15), doc='Temperature of the D-labelling reaction',
                               label='Temperature (K)')
    pH = param.Number(7.5, doc='pH of the D-labelling reaction, as read from pH meter',
                      label='pH read')
    #load_button = param.Action(lambda self: self._action_load(), doc='Load the selected files', label='Load Files')

    c_term = param.Integer(0, bounds=(0, None),
                           doc='Index of the c terminal residue in the protein. Used for generating pymol export script'
                               'and determination of intrinsic rate of exchange for the C-terminal residue')
    n_term = param.Integer(1, doc='Index of the n terminal residue in the protein. Can be set to negative values to '
                                  'accommodate for purification tags. Used in the determination of intrinsic rate of exchange')
    sequence = param.String('', doc='Optional FASTA protein sequence')
    dataset_name = param.String()
    add_dataset_button = param.Action(lambda self: self._action_add_dataset(), label='Add dataset',
                                doc='Parse selected peptides for further analysis and apply back-exchange correction')
    dataset_list = param.ListSelector(label='Datasets', doc='Lists available datasets')

    def __init__(self, parent, **params):
        super(PeptideFileInputControl, self).__init__(parent, **params)
        self.parent.param.watch(self._datasets_updated, ['fit_objects'])

        widgets = [name for name in self.widgets.keys() if name not in ['be_percent']]
        self._layout = {'self': widgets}
        self.update_box()

        self._array = None  # Numpy array with raw input data

    def make_dict(self):
        text_area = pn.widgets.TextAreaInput(name='Sequence (optional)', placeholder='Enter sequence in FASTA format', max_length=10000,
                                             width=300, height=100, height_policy='fixed', width_policy='fixed')
        return self.generate_widgets(
            input_files=pn.widgets.FileInput(multiple=True, name='Input files'),
            temperature=pn.widgets.FloatInput,
            #be_mode=pn.widgets.RadioButtonGroup,
            be_percent=pn.widgets.FloatInput,
            d_percentage=pn.widgets.FloatInput,
            fd_percentage=pn.widgets.FloatInput,
            sequence=text_area)

    def make_list(self):
        excluded = ['be_percent']
        widget_list = [widget for name, widget, in self.widget_dict.items() if name not in excluded]

        return widget_list

    def _action_add_dataset(self):
        """Apply controls to :class:`~pyhdx.models.PeptideMasterTable` and set :class:`~pyhdx.models.KineticsSeries`"""

        if self._array is None:
            return

        peptides = PeptideMasterTable(self._array, d_percentage=self.d_percentage,
                                      drop_first=self.drop_first, ignore_prolines=self.ignore_prolines)
        if self.be_mode == 'FD Sample':
            control_0 = None # = (self.zero_state, self.zero_exposure) if self.zero_state != 'None' else None
            peptides.set_control((self.fd_state, self.fd_exposure), control_0=control_0)
        elif self.be_mode == 'Flat percentage':
            # todo @tejas: Add test
            peptides.set_backexchange(self.be_percent)

        data_states = peptides.data[peptides.data['state'] == self.exp_state]
        data = data_states[np.isin(data_states['exposure'], self.exp_exposures)]

        series = KineticsSeries(data, c_term=self.c_term, n_term=self.n_term, sequence=self.sequence)
        kf = KineticsFitting(series, temperature=self.temperature, pH=self.pH, cluster=self.parent.cluster)
        self.parent.fit_objects[self.dataset_name] = kf
        self.parent.param.trigger('fit_objects')  # Trigger update

        df = pd.DataFrame(series.full_data)
        target_source = self.parent.sources['dataframe']
        target_source.add_df(df, 'peptides', self.dataset_name)

        self.parent.logger.info(f'Loaded dataset {self.dataset_name} with experiment state {self.exp_state} '
                                f'({len(series)} timepoints, {len(series.coverage)} peptides each)')
        self.parent.logger.info(f'Average coverage: {series.coverage.percent_coverage:.3}%, '
                                f'Redundancy: {series.coverage.redundancy:.2}')

    @param.depends('be_mode', watch=True)
    def _update_be_mode(self):
        # todo @tejas: Add test
        if self.be_mode == 'FD Sample':
            excluded = ['be_percent']
        elif self.be_mode == 'Flat percentage':
            excluded = ['fd_state', 'fd_exposure']

        widgets = [name for name in self.widgets.keys() if name not in excluded]
        self._layout = {'self': widgets}
        self.update_box()

    @param.depends('input_files', watch=True)
    def _read_files(self):
        """"""
        if self.input_files:
            combined_array = read_dynamx(*[StringIO(byte_content.decode('UTF-8')) for byte_content in self.input_files])
            self._array = combined_array

            self.parent.logger.info(
                f'Loaded {len(self.input_files)} file{"s" if len(self.input_files) > 1 else ""} with a total '
                f'of {len(self._array)} peptides')

        else:
            self._array = None

        self._update_fd_state()
        self._update_fd_exposure()
        self._update_exp_state()
        self._update_exp_exposure()

    def _update_fd_state(self):
        if self._array is not None:
            states = list(np.unique(self._array['state']))
            self.param['fd_state'].objects = states
            self.fd_state = states[0]
        else:
            self.param['fd_state'].objects = []

    @param.depends('fd_state', watch=True)
    def _update_fd_exposure(self):
        if self._array is not None:
            fd_entries = self._array[self._array['state'] == self.fd_state]
            exposures = list(np.unique(fd_entries['exposure']))
        else:
            exposures = []
        self.param['fd_exposure'].objects = exposures
        if exposures:
            self.fd_exposure = exposures[0]

    @param.depends('fd_state', 'fd_exposure', watch=True)
    def _update_exp_state(self):
        if self._array is not None:
            # Booleans of data entries which are in the selected control
            control_bools = np.logical_and(self._array['state'] == self.fd_state, self._array['exposure'] == self.fd_exposure)

            control_data = self._array[control_bools]
            other_data = self._array[~control_bools]

            intersection = array_intersection([control_data, other_data], fields=['start', 'end'])  # sequence?
            states = list(np.unique(intersection[1]['state']))
        else:
            states = []

        self.param['exp_state'].objects = states
        if states:
            self.exp_state = states[0] if not self.exp_state else self.exp_state

    @param.depends('exp_state', watch=True)
    def _update_exp_exposure(self):
        if self._array is not None:
            exp_entries = self._array[self._array['state'] == self.exp_state]
            exposures = list(np.unique(exp_entries['exposure']))
            exposures.sort()
        else:
            exposures = []

        self.param['exp_exposures'].objects = exposures
        self.exp_exposures = exposures

        if not self.dataset_name or self.dataset_name in self.param['exp_state'].objects:
            self.dataset_name = self.exp_state

        if not self.c_term and exposures:
            self.c_term = int(np.max(exp_entries['end']))

    def _datasets_updated(self, events):
        # Update datasets widget as datasets on parents change
        objects = list(self.parent.fit_objects.keys())
        self.param['dataset_list'].objects = objects

    def _action_remove_datasets(self):
        for name in self.dataset_list:
            print(name)
            self.parent.datasets.pop(name)

        self.parent.param.trigger('datasets')  # Manual trigger as key assignment does not trigger the param


class CoverageControl(ControlPanel):
    header = 'Coverage'

    temp_new_data = param.Action(lambda self: self._action_new_data())

    def __init__(self, parent, **params):
        super().__init__(parent, **params)

        self._layout = {
            'filters.select_index': None,
            'filters.exposure_slider': None,
            'self': None
        }

        self.update_box()

    def _action_new_data(self):
        df = csv_to_dataframe(r'C:\Users\jhsmi\pp\PyHDX\tests\test_data\ecSecB_apo_peptides.csv')

        print(len(df))
        reduced_df = df.query('exposure < 50')
        print(len(reduced_df))

        source = self.sources['dataframe']

        source.add_df(reduced_df, 'peptides', 'ecSecB_reduced')


class InitialGuessControl(ControlPanel):
    """
    This controller allows users to derive initial guesses for D-exchange rate from peptide uptake data.
    """

    #todo remove lambda symbol although its really really funny
    header = 'Initial Guesses'
    fitting_model = param.Selector(default='Half-life (λ)', objects=['Half-life (λ)', 'Association'],
                                   doc='Choose method for determining initial guesses.')
    dataset = param.Selector(default='', doc='Dataset to apply bounds to')
    global_bounds = param.Boolean(default=False, doc='Set bounds globally across all datasets')
    lower_bound = param.Number(0., doc='Lower bound for association model fitting')
    upper_bound = param.Number(0., doc='Upper bound for association model fitting')
    do_fit1 = param.Action(lambda self: self._action_fit(), label='Do fitting', doc='Start initial guess fitting',
                           constant=True)

    def __init__(self, parent, **params):
        self.pbar1 = ASyncProgressBar()  #tqdm? https://github.com/holoviz/panel/pull/2079
        self.pbar2 = ASyncProgressBar()
        super(InitialGuessControl, self).__init__(parent, **params)
        self.parent.param.watch(self._parent_datasets_updated, ['fit_objects'])  #todo refactor

        excluded = ['lower_bound', 'upper_bound', 'global_bounds', 'dataset']
        widgets = [name for name in self.widgets.keys() if name not in excluded]
        self._layout = {'self': widgets}
        self.update_box()

    def make_dict(self):
        widgets = self.generate_widgets(lower_bound=pn.widgets.FloatInput, upper_bound=pn.widgets.FloatInput)
        widgets.update(pbar1=self.pbar1.view, pbar2=self.pbar2.view)

        return widgets

    # def make_list(self):
    #     self.widget_dict.update(pbar1=self.pbar1.view, pbar2=self.pbar2.view)
    #     parameters = ['fitting_model', 'do_fit1', 'pbar1']
    #
    #     widget_list = list([self.widget_dict[par] for par in parameters])
    #     return widget_list

    @param.depends('fitting_model', watch=True)
    def _fitting_model_updated(self):
        if self.fitting_model == 'Half-life (λ)':
            excluded = ['lower_bound', 'upper_bound', 'global_bounds']

        elif self.fitting_model in ['Association', 'Dissociation']:
            excluded = []

        widgets = [name for name in self.widgets.keys() if name not in excluded]
        print('widgets in updated', widgets)
        self._layout = {'self': widgets}

        self.update_box()

    @param.depends('global_bounds', watch=True)
    def _global_bounds_updated(self):
        if self.global_bounds:
            self.param['dataset'].constant = True
        else:
            self.param['dataset'].constant = False

    @param.depends('dataset', watch=True)
    def _dataset_updated(self):
        kf = self.parent.fit_objects[self.dataset]
        lower, upper = kf.bounds
        self.lower_bound = lower
        self.upper_bound = upper

    @param.depends('lower_bound', watch=True)
    def _lower_bound_updated(self):
        #this works but maybe not ideal
        # set param?
        if self.global_bounds:
            kfs = self.parent.fit_objects.values()
        else:
            kfs = [self.parent.fit_objects[self.dataset]]

        for kf in kfs:
            lower, upper = kf.bounds
            kf.bounds = (self.lower_bound, upper)

    @param.depends('upper_bound', watch=True)
    def _upper_bound_updated(self):
        if self.global_bounds:
            kfs = self.parent.fit_objects.values()
        else:
            kfs = [self.parent.fit_objects[self.dataset]]

        for kf in kfs:
            lower, upper = kf.bounds
            kf.bounds = (lower, self.upper_bound)

    def _parent_datasets_updated(self, events):
        if len(self.parent.fit_objects) > 0:
            self.param['do_fit1'].constant = False

        options = list(self.parent.fit_objects.keys())
        self.param['dataset'].objects = options
        if not self.dataset:
            self.dataset = options[0]

    @staticmethod
    def fit_result_dict_to_df(results):

        combined_results = pd.concat(results.values(), axis=1,
                                     keys=list(results.keys()),
                                     names=['state', 'quantity'])

        return combined_results

    async def _fit1_async(self, output_name):
        """Do fitting asynchronously on (remote) cluster"""
        results = {}
        for name, kf in self.parent.fit_objects.items():
            fit_result = await kf.weighted_avg_fit_async(model_type=self.fitting_model.lower(), pbar=self.pbar1)
            results[kf.series.state] = fit_result

        self.parent.fit_results['fit_1'] = results  #todo refactor 'fit1' to guess
#       self.parent.param.trigger('fit_results')

        dfs = [result.output.df for result in results.values()]  # todo get r_number as column? or as index?
        combined_results = pd.concat(dfs, axis=1,
                                     keys=list(results.keys()),
                                     names=['state', 'quantity'])

        def add_df(source, df, table):
            source.add_df(df, table)

        callback = partial(self.sources['dataframe'], add_df, combined_results, 'rates')
        self.parent.doc.add_next_tick_callback(callback)

        with pn.io.unlocked():
             self.parent.param.trigger('fit_results')  #informs other fittings that initial guesses are now available
             self.pbar1.reset()
             self.param['do_fit1'].constant = False

    def _fit1(self):
        results = {}
        for name, kf in self.parent.fit_objects.items():
            fit_result = kf.weighted_avg_fit(model_type=self.fitting_model.lower(), pbar=self.pbar1)
            results[kf.series.state] = fit_result

        self.parent.fit_results['fit1'] = results
        self.parent.param.trigger('fit_results')

        dfs = [result.output for result in results.values()]
        combined_results = pd.concat(dfs, axis=1,
                                     keys=list(results.keys()),
                                     names=['state', 'quantity'])



        self.param['do_fit1'].constant = False
        self.pbar1.reset()

    def _action_fit(self):
        if len(self.parent.fit_objects) == 0:
            self.parent.logger.debug('No datasets loaded')
            return

        self.parent.logger.debug('Start initial guess fit')
        #todo context manager?
        self.param['do_fit1'].constant = True

        if self.fitting_model == 'Half-life (λ)':
            results = {}
            for name, kf in self.parent.fit_objects.items():
                fit_result = kf.weighted_avg_t50()
                results[name] = fit_result

            self.parent.fit_results['half-life'] = results
            self.parent.param.trigger('fit_results')  # Informs TF fitting that now fit1 is available as initial guesses

            dfs = [result.output.df for result in results.values()]
            # Resulting df has Int64Index as index with name 'r_number'
            combined_results = pd.concat(dfs, axis=1,
                                         keys=list(results.keys()),
                                         names=['state', 'quantity'])

            self.sources['dataframe'].tables['half-life'] = combined_results
            self.sources['dataframe'].updated = True

            self.param['do_fit1'].constant = False
        else:

            if self.parent.cluster:
                self.parent._doc = pn.state.curdoc
                loop = IOLoop.current()
                loop.add_callback(self._fit1_async)
            else:
                self._fit1()


class SingleMappingFileInputControl(MappingFileInputControl):
    """
    This controller allows users to upload *.txt files where quantities (protection factors, Gibbs free energy, etc) are
    mapped to a linear sequence.

    The column should be tab separated with on the last header line (starts with '#') the names of the columns. Columns
    should be tab-delimited.
    """

    def _action_add_dataset(self):
        super()._action_add_dataset()
        to_add_keys = set(self.parent.datasets.keys()) - set(self.parent.sources.keys())
        for key in to_add_keys:
            records = self.parent.datasets[key].to_records()
            data_source = DataSource(records, tags=['comparison', 'mapping'], x='r_number',
                                     renderer='circle', size=10)
            self.parent.publish_data(key, data_source)


class MatrixMappingFileInputControl(SingleMappingFileInputControl):
    datapoints = param.ListSelector(doc='Select datapoints to include in the matrix')

    def _action_add_dataset(self):
        super()._action_add_dataset()

        N = 20
        img = np.empty((N, N), dtype=np.uint32)
        view = img.view(dtype=np.uint8).reshape((N, N, 4))
        for i in range(N):
            for j in range(N):
                view[i, j, 0] = int(i / N * 255)
                view[i, j, 1] = 158
                view[i, j, 2] = int(j / N * 255)
                view[i, j, 3] = 255

        values = np.random.random(img.shape)

        img_ds_dict = {'img': [img], 'scores': [values]}
        data_source = DataSource(img_ds_dict, tags=['image'], name='scores_image', x=0, y=0)

        self.parent.publish_data('scores_image', data_source)

    def make_list(self):
        widget_list = super().make_list()
        datapoints_widget = widget_list.pop()
        widget_list.insert(3, datapoints_widget)
        return widget_list

    def _add_dataset(self):
        full_dict = self.protein.to_dict()
        data_dict = {k: v for k, v in full_dict.items() if k in self.datapoints}
        data_dict['r_number'] = self.protein.index
        protein = Protein(data_dict, index='r_number')
        self.parent.datasets[self.dataset_name] = protein

    @param.depends('input_file', watch=True)
    def _input_file_updated(self):
        super()._input_file_updated()
        if self.input_file:
            header_fields = self.protein.df.columns

            float_fields = [f for f in header_fields if f.replace('.', '', 1).isdigit()]
            self.param['datapoints'].objects = float_fields
            self.datapoints = float_fields

#        self.dataset_name = self.dataset_name or Path(self.widget_dict['input_file'].filename).stem

class MatrixImageControl(ControlPanel):
    """
    This controller takes an input loaded matrix and converts it to an (rgba) interpolated rendered image

    """


class FDPeptideFileInputControl(PeptideFileInputControl):
    # todo @tejas: Add test
    # This requires making a test function with the full_deuteration_app in apps.py
    def make_list(self):
        parameters = ['add_button', 'clear_button', 'drop_first', 'load_button', 'd_percentage',
                      'fd_state', 'fd_exposure', 'parse_button']
        first_widgets = list([self.widget_dict[par] for par in parameters])
        return self.file_selectors + first_widgets

    def _action_parse(self):
        """Apply controls to :class:`~pyhdx.models.PeptideMasterTable` and set :class:`~pyhdx.models.KineticsSeries`"""
        pmt = self.parent.peptides

        data_states = pmt.data[pmt.data['state'] == self.fd_state]
        data_exposure = data_states[data_states['exposure'] == self.fd_exposure]

        scores = 100 * data_exposure['uptake'] / data_exposure['ex_residues']
        data_final = append_fields(data_exposure, 'scores', data=scores, usemask=False)

        # pmt.set_control((fd_state, fd_exposure))
        series = KineticsSeries(data_final)

        self.parent.series = series

        self.parent.logger.info(f"Loaded FD control '{self.exp_state}' with {len(series.coverage)} peptides")
        self.parent.logger.info(f'Mean deuteration is {scores.mean()}%, std {scores.std()}%')


class PeptideFoldingFileInputControl(PeptideFileInputControl):
    # todo @tejas: Add test
    # This requires making a test function with the folding in apps.py

    be_mode = param.Selector(doc='Select method of normalization', label='Norm mode', objects=['Exp', 'Theory']
                             , precedence=-1)
    fd_state = param.Selector(doc='State used to normalize uptake', label='100% Control State')
    fd_exposure = param.Selector(doc='Exposure used to normalize uptake', label='100% Control Exposure')
    zero_state = param.Selector(doc='State used to zero uptake', label='0% Control State')
    zero_exposure = param.Selector(doc='Exposure used to zero uptake', label='0% Control Exposure')

    def make_dict(self):
        return self.generate_widgets()

    def make_list(self):
        parameters = ['add_button', 'clear_button', 'drop_first', 'ignore_prolines', 'load_button',
                      'fd_state', 'fd_exposure', 'zero_state', 'zero_exposure', 'exp_state',
                      'exp_exposures', 'parse_button']
        first_widgets = list([self.widget_dict[par] for par in parameters])
        return self.file_selectors + first_widgets

    def _action_load(self):
        super()._action_load()
        states = list(np.unique(self.parent.peptides.data['state']))
        self.param['zero_state'].objects = states
        self.zero_state = states[0]

    @param.depends('fd_state', 'fd_exposure', watch=True)
    def _update_experiment(self):
        #TODO THIS needs to be updated to also incorporate the zero (?)
        pm_dict = self.parent.peptides.return_by_name(self.fd_state, self.fd_exposure)
        states = list(np.unique([v.state for v in pm_dict.values()]))
        self.param['exp_state'].objects = states
        self.exp_state = states[0] if not self.exp_state else self.exp_state

    @param.depends('zero_state', watch=True)
    def _update_zero_exposure(self):
        b = self.parent.peptides.data['state'] == self.zero_state
        data = self.parent.peptides.data[b]
        exposures = list(np.unique(data['exposure']))
        self.param['zero_exposure'].objects = exposures
        if exposures:
            self.control_exposure = exposures[0]

    def _action_parse(self):
        """Apply controls to :class:`~pyhdx.models.PeptideMasterTable` and set :class:`~pyhdx.models.KineticsSeries`"""
        control_0 = self.zero_state, self.zero_exposure
        self.parent.peptides.set_control((self.fd_state, self.fd_exposure), control_0=control_0)

        data_states = self.parent.peptides.data[self.parent.peptides.data['state'] == self.exp_state]
        data = data_states[np.isin(data_states['exposure'], self.exp_exposures)]

        series = KineticsSeries(data)
        self.parent.series = series

        self._publish_scores()

        self.parent.logger.info(f'Loaded experiment state {self.exp_state} '
                                f'({len(series)} timepoints, {len(series.coverage)} peptides each)')


class DifferenceControl(ControlPanel):
    """
    This controller allows users to select two datasets from available datasets, choose a quantity to compare between,
    and choose the type of operation between quantities (Subtract/Divide).

    """
    header = 'Differences'

    dataset_1 = param.Selector(doc='First dataset to compare')
    dataset_2 = param.Selector(doc='Second dataset to compare')

    comparison_name = param.String()
    operation = param.Selector(default='Subtract', objects=['Subtract', 'Divide'],
                               doc='Select the operation to perform between the two datasets')

    comparison_quantity = param.Selector(doc="Select a quantity to compare (column from input txt file)")
    add_comparison = param.Action(lambda self: self._action_add_comparison(),
                                  doc='Click to add this comparison to available comparisons')
    comparison_list = param.ListSelector(doc='Lists available comparisons')
    remove_comparison = param.Action(lambda self: self._action_remove_comparison(),
                                     doc='Remove selected comparisons from the list')

    def __init__(self, parent, **params):
        super(DifferenceControl, self).__init__(parent, **params)
        self.parent.param.watch(self._datasets_updated, ['datasets'])

    def _datasets_updated(self, events):
        objects = list(self.parent.datasets.keys())

        self.param['dataset_1'].objects = objects
        if not self.dataset_1:
            self.dataset_1 = objects[0]
        self.param['dataset_2'].objects = objects
        if not self.dataset_2:# or self.dataset_2 == objects[0]:  # dataset2 default to second dataset? toggle user modify?
            self.dataset_2 = objects[0]

    @param.depends('dataset_1', 'dataset_2', watch=True)
    def _selection_updated(self):
        if self.datasets:
            unique_names = set.intersection(*[{name for name in protein.df.dtypes.index} for protein in self.datasets])
            objects = [name for name in unique_names if np.issubdtype(self.protein_1[name].dtype, np.number)]
            objects.sort()

            # todo check for scara dtype
            self.param['comparison_quantity'].objects = objects
            if self.comparison_quantity is None:
                self.comparison_quantity = objects[0]

    @property
    def protein_1(self):
        """:class:`~pyhdx.models.Protein`: Protein object of dataset 1"""
        try:
            return self.parent.datasets[self.dataset_1]
        except KeyError:
            return None

    @property
    def protein_2(self):
        """:class:`~pyhdx.models.Protein`: Protein object of dataset 2"""
        try:
            return self.parent.datasets[self.dataset_2]
        except KeyError:
            return None

    @property
    def datasets(self):
        """:obj:`tuple`: Tuple with `(protein_1, protein_2)"""
        datasets = (self.protein_1, self.protein_2)
        if None in datasets:
            return None
        else:
            return datasets

    def _action_add_comparison(self):
        if not self.comparison_name:
            self.parent.logger.info('The added comparison needs to have a name')
            return
        if self.datasets is None:
            return

        op = {'Subtract': operator.sub, 'Divide': operator.truediv}[self.operation]
        comparison = op(*[p[self.comparison_quantity] for p in self.datasets]).rename('comparison')
        value1 = self.protein_1[self.comparison_quantity].rename('value1')
        value2 = self.protein_2[self.comparison_quantity].rename('value2')
        df = pd.concat([comparison, value1, value2], axis=1)

        output = df.to_records()
        data_source = DataSource(output, tags=['comparison', 'mapping'], x='r_number', y='comparison',
                                 renderer='circle', size=10)
        self.parent.publish_data(self.comparison_name, data_source)  # Triggers parent.sources param
        self.comparison_name = ''

    def _action_remove_comparison(self):
        for comparison in self.comparison_list:
            self.parent.sources.pop(comparison)   #Popping from dicts does not trigger param
        self.parent.param.trigger('sources')

    @param.depends('parent.sources', watch=True)
    def _update_comparison_list(self):
        objects = [name for name, d in self.parent.sources.items() if 'comparison' in d.tags]
        self.param['comparison_list'].objects = objects


class SingleControl(ControlPanel):
    # todo @tejas: Add test

    """
    This controller allows users to select a dataset from available datasets, and choose a quantity to classify/visualize,
    and add this quantity to the available datasets.
    """

    #todo subclass with DifferenceControl
    #rename dataset_name
    header = 'Datasets'

    dataset = param.Selector(doc='Dataset')
    dataset_name = param.String(doc='Name of the dataset to add')
    quantity = param.Selector(doc="Select a quantity to plot (column from input txt file)")

    add_dataset = param.Action(lambda self: self._action_add_dataset(),
                               doc='Click to add this comparison to available comparisons')
    dataset_list = param.ListSelector(doc='Lists available comparisons')
    remove_dataset = param.Action(lambda self: self._action_remove_comparison(),
                                  doc='Remove selected datasets from available datasets')

    def __init__(self, parent, **params):
        super(SingleControl, self).__init__(parent, **params)
        self.parent.param.watch(self._datasets_updated, ['datasets'])

    def _datasets_updated(self, events):
        objects = list(self.parent.datasets.keys())

        self.param['dataset'].objects = objects
        if not self.dataset:
            self.dataset = objects[0]

    @param.depends('dataset', watch=True)
    def _selection_updated(self):
        if self.dataset:
            dataset = self.parent.datasets[self.dataset]
            names = dataset.dtype.names
            objects = [name for name in names if name != 'r_number']
            self.param['quantity'].objects = objects
            if self.quantity is None:
                self.quantity = objects[0]

    def _action_add_dataset(self):
        if not self.dataset_name:
            self.parent.logger.info('The added comparison needs to have a name')
            return
        if not self.dataset:
            return

        array = self.parent.datasets[self.dataset]
        data_source = DataSource(array, tags=['comparison', 'mapping'], x='r_number', y=self.quantity,
                                 renderer='circle', size=10)
        self.parent.publish_data(self.dataset_name, data_source)  # Triggers parent.sources param
        self.comparison_name = ''

    def _action_remove_comparison(self):
        for ds in self.dataset_list:
            self.parent.sources.pop(ds)   #Popping from dicts does not trigger param
        self.parent.param.trigger('sources')

    @param.depends('parent.sources', watch=True)
    def _update_dataset_list(self):
        objects = [name for name, d in self.parent.sources.items()]
        self.param['dataset_list'].objects = objects


class FDCoverageControl(CoverageControl):
    def make_list(self):
        lst = super(CoverageControl, self).make_list()
        return lst[:-1]




class FoldingFitting(InitialGuessControl):
    fitting_model = param.Selector(default='Dissociation', objects=['Dissociation'],
                                   doc='Choose method for determining initial guesses.')

    def make_list(self):
        self.widget_dict.update(pbar1=self.pbar1.view, pbar2=self.pbar2.view)
        parameters = ['fitting_model', 'lower_bound', 'upper_bound', 'do_fit1', 'pbar1']

        widget_list = list([self.widget_dict[par] for par in parameters])
        return widget_list


class FitControl(ControlPanel):
    """
    This controller allows users to execute TensorFlow fitting of the global data set.

    Currently, repeated fitting overrides the old result.
    """

    header = 'Fitting'
    initial_guess = param.Selector(doc='Name of dataset to use for initial guesses.')
    temperature = param.Number(293.15, doc='Deuterium labelling temperature in Kelvin')
    pH = param.Number(8., doc='Deuterium labelling pH', label='pH')

    stop_loss = param.Number(0.01, bounds=(0, None),
                             doc='Threshold loss difference below which to stop fitting.')
    stop_patience = param.Integer(100, bounds=(1, None),
                                  doc='Number of epochs where stop loss should be satisfied before stopping.')
    learning_rate = param.Number(10, bounds=(0, None),
                                 doc='Learning rate parameter for optimization.')
    momentum = param.Number(0.5, bounds=(0, None),
                            doc='Stochastic Gradient Descent momentum')
    nesterov = param.Boolean(True, doc='Use Nesterov type of momentum for SGD')
    epochs = param.Number(100000, bounds=(1, None),
                          doc='Maximum number of epochs (iterations.')
    regularizer = param.Number(0.5, bounds=(0, None), doc='Value for the regularizer.')
    do_fit = param.Action(lambda self: self._action_fit(), constant=True, label='Do Fitting',
                          doc='Start global fitting')

    def __init__(self, parent, **params):
        self.pbar1 = ASyncProgressBar()
        super(FitControl, self).__init__(parent, **params)
        self.parent.param.watch(self._parent_fit_results_updated, ['fit_results'])

    def _parent_fit_results_updated(self, *events):
        possible_initial_guesses = ['half-life', 'fit1']
        objects = [name for name in possible_initial_guesses if name in self.parent.fit_results.keys()]
        if objects:
            self.param['do_fit'].constant = False

        self.param['initial_guess'].objects = objects
        if not self.initial_guess and objects:
            self.initial_guess = objects[0]

    @staticmethod
    def result_to_data_source(output):
        output.df['color'] = np.full(len(output), fill_value=DEFAULT_COLORS['pfact'], dtype='<U7') #todo change how default colors are determined

        # Add upper/lower bounds covariances for error bar plotting
        output.df['__lower'] = output.df['deltaG'] - output.df['covariance']
        output.df['__upper'] = output.df['deltaG'] + output.df['covariance']

        output_name = 'global_fit'  # Appears twice
        data_source = DataSource(output, x='r_number', tags=['mapping', 'pfact', 'deltaG'], name=output_name,
                                 renderer='circle', size=10)

        return data_source

    async def _do_fitting_async(self):
        kf = KineticsFitting(self.parent.series, temperature=self.temperature, pH=self.pH, cluster=self.parent.cluster)
        initial_result = self.parent.fit_results[self.initial_guess].output

        result = await kf.global_fit_async(initial_result, r1=self.regularizer, lr=self.learning_rate,
                                           momentum=self.momentum, nesterov=self.nesterov, epochs=self.epochs,
                                           patience=self.stop_patience, stop_loss=self.stop_loss)

        # Duplicate code
        self.parent.logger.info('Finished PyTorch fit')
        loss = result.metadata['mse_loss']
        self.parent.logger.info(f"Finished fitting in {len(loss)} epochs, final mean squared residuals is {result.mse_loss:.2f}")
        self.parent.logger.info(f"Total loss: {result.total_loss:.2f}, regularization loss: {result.reg_loss:.2f} "
                                f"({result.regularization_percentage:.1f}%)")

        self.parent.param.trigger('fit_results')

        data_source = self.result_to_data_source(result.output)
        output_name = 'global_fit'
        callback = partial(self.parent.publish_data, output_name, data_source)
        self.parent.doc.add_next_tick_callback(callback)

        self.parent.fit_results['fr_' + output_name] = result
        with pn.io.unlocked():
             self.parent.param.trigger('fit_results')  #informs other fittings that initial guesses are now available
             self.widget_dict['do_fit'].loading = False

    def _do_fitting(self):
        kf = KineticsFitting(self.parent.series, temperature=self.temperature, pH=self.pH)
        initial_result = self.parent.fit_results[self.initial_guess].output   #todo initial guesses could be derived from the CDS rather than fit results object
        result = kf.global_fit(initial_result, r1=self.regularizer, lr=self.learning_rate,
                               momentum=self.momentum, nesterov=self.nesterov, epochs=self.epochs,
                               patience=self.stop_patience, stop_loss=self.stop_loss)

        self.parent.logger.info('Finished PyTorch fit')
        loss = result.metadata['mse_loss']
        self.parent.logger.info(f"Finished fitting in {len(loss)} epochs, final mean squared residuals is {result.mse_loss:.2f}")
        self.parent.logger.info(f"Total loss: {result.total_loss:.2f}, regularization loss: {result.reg_loss:.2f} "
                                f"({result.regularization_percentage:.1f}%)")

        self.parent.param.trigger('fit_results')

        data_source = self.result_to_data_source(result.output)
        output_name = 'global_fit'
        self.parent.fit_results['fr_' + output_name] = result
        self.parent.publish_data(output_name, data_source)

        self.widget_dict['do_fit'].loading = False

    def _action_fit(self):
        self.widget_dict['do_fit'].loading = True
        self.parent.logger.debug('Start PyTorch fit')

        if self.parent.cluster:
            self.parent._doc = pn.state.curdoc
            loop = IOLoop.current()
            loop.add_callback(self._do_fitting_async)
        else:
            self._do_fitting()


class FitResultControl(ControlPanel):
    # @tejas skip test, currently bugged, issue #182

    """
    This controller allows users to view to fit result and how it describes the uptake of every peptide.
    """

    header = 'Fit Results'

    peptide_index = param.Integer(0, bounds=(0, None),
                                 doc='Index of the peptide to display.')
    x_axis_type = param.Selector(default='Log', objects=['Linear', 'Log'],
                                 doc='Choose whether to plot the x axis as Logarithmic axis or Linear.')

    def __init__(self, parent, **param):
        super(FitResultControl, self).__init__(parent, **param)

        self.d_uptake = {}  ## Dictionary of arrays (N_p, N_t) with results of fit result model calls
        #todo why does still still exists should it not just be dataobjects??
        # --> because they need to be calcualted only once and then dataobjects are generated per index
        # can be improved probably (by putting all data in data source a priory?

        self.parent.param.watch(self._series_updated, ['datasets']) #todo refactor
        self.parent.param.watch(self._fit_results_updated, ['fit_results'])

    def _series_updated(self, *events):
        print('update')
        #
        # self.param['peptide_index'].bounds = (0, len(self.parent.series.coverage.data) - 1)
        # self.d_uptake['uptake_corrected'] = self.parent.series.uptake_corrected.T
        # self._update_sources()

    @property
    def fit_timepoints(self):
        time = np.logspace(-2, np.log10(self.parent.series.timepoints.max()), num=250)
        time = np.insert(time, 0, 0.)
        return time

    def _fit_results_updated(self, *events):
        accepted_fitresults = ['fr_pfact']
        #todo wrappertje which checks with a cached previous version of this particular param what the changes are even it a manual trigger
        for name, fit_result in self.parent.fit_results.items():
            if name in accepted_fitresults:
                D_upt = fit_result(self.fit_timepoints)
                self.d_uptake[name] = D_upt
            else:
                continue
        # push results to graph
            self._update_sources()

    @param.depends('peptide_index', watch=True)
    def _update_sources(self):
        for name, array in self.d_uptake.items():
            if name == 'uptake_corrected':  ## this is the raw data
                timepoints = self.parent.series.timepoints
                renderer = 'circle'
                color = '#000000'
            else:
                timepoints = self.fit_timepoints
                renderer = 'line'
                color = '#bd0d1f'  #todo css / default color cycle per Figure Panel?

            dic = {'time': timepoints, 'uptake': array[self.peptide_index, :]}
            data_source = DataSource(dic, x='time', y='uptake', tags=['uptake_curve'], renderer=renderer, color=color)
            self.parent.publish_data(name, data_source)


class ClassificationControl(ControlPanel):
    """
    This controller allows users classify 'mapping' datasets and assign them colors.

    Coloring can be either in discrete categories or as a continuous custom color map.
    """

    header = 'Classification'
    # format ['tag1', ('tag2a', 'tag2b') ] = tag1 OR (tag2a AND tag2b)
    accepted_tags = ['mapping']

    # todo unify name for target field (target_data set)
    # When coupling param with the same name together there should be an option to exclude this behaviour
    target = param.Selector(label='Target')
    quantity = param.Selector(label='Quantity')

    mode = param.Selector(default='Discrete', objects=['Discrete', 'Continuous'],
                          doc='Choose color mode (interpolation between selected colors).')#, 'ColorMap'])
    num_colors = param.Integer(3, bounds=(1, 10),
                              doc='Number of classification colors.')
    otsu_thd = param.Action(lambda self: self._action_otsu(), label='Otsu',
                            doc="Automatically perform thresholding based on Otsu's method.")
    linear_thd = param.Action(lambda self: self._action_linear(), label='Linear',
                              doc='Automatically perform thresholding by creating equally spaced sections.')
    log_space = param.Boolean(True,
                              doc='Boolean to set whether to apply colors in log space or not.')

    show_thds = param.Boolean(True, label='Show Thresholds', doc='Toggle to show/hide threshold lines.')
    values = param.List(precedence=-1)
    colors = param.List(precedence=-1)

    def __init__(self, parent, **param):
        super(ClassificationControl, self).__init__(parent, **param)

        self.values_widgets = []
        self.colors_widgets = []
        self._update_num_colors()
        self._update_num_values()

        self.param.trigger('values')
        self.param.trigger('colors')
        self.parent.param.watch(self._parent_sources_updated, ['sources'])

    def make_dict(self):
        return self.generate_widgets(num_colors=pn.widgets.IntInput, mode=pn.widgets.RadioButtonGroup)

    def _parent_sources_updated(self, *events):
        data_sources = [k for k, src in self.parent.sources.items() if src.resolve_tags(self.accepted_tags)]
        self.param['target'].objects = list(data_sources)

        # Set target if its not set already
        if not self.target and data_sources:
            self.target = data_sources[-1]

        if self.values:
            self._get_colors()

    @param.depends('target', watch=True)
    def _target_updated(self):
        data_source = self.parent.sources[self.target]
        self.param['quantity'].objects = [f for f in data_source.scalar_fields if not f.startswith('_')]
        default_priority = ['deltaG', 'comparison']  # Select these fields by default if they are present
        if not self.quantity and data_source.scalar_fields:
            for field in default_priority:
                if field in data_source.scalar_fields:
                    self.quantity = field
                    break
                self.quantity = data_source.scalar_fields[0]

    @property
    def target_array(self):
        """returns the array to calculate colors from, NaN entries are removed"""

        try:
            y_vals = self.parent.sources[self.target][self.quantity]
            return y_vals[~np.isnan(y_vals)]
        except KeyError:
            return None

    def _action_otsu(self):
        if self.num_colors > 1 and self.target_array is not None:
            func = np.log if self.log_space else lambda x: x  # this can have NaN when in log space
            thds = threshold_multiotsu(func(self.target_array), classes=self.num_colors)
            for thd, widget in zip(thds[::-1], self.values_widgets):  # Values from high to low
                widget.start = None
                widget.end = None
                widget.value = np.exp(thd) if self.log_space else thd
        self._update_bounds()
        self._get_colors()

    def _action_linear(self):
        i = 1 if self.mode == 'Discrete' else 0
        if self.log_space:
            thds = np.logspace(np.log(np.min(self.target_array)), np.log(np.max(self.target_array)),
                               num=self.num_colors + i, endpoint=True, base=np.e)
        else:
            thds = np.linspace(np.min(self.target_array), np.max(self.target_array), num=self.num_colors + i, endpoint=True)
        for thd, widget in zip(thds[i:self.num_colors][::-1], self.values_widgets):
            # Remove bounds, set values, update bounds
            widget.start = None
            widget.end = None
            widget.value = thd
        self._update_bounds()

    @param.depends('mode', watch=True)
    def _mode_updated(self):
        if self.mode == 'Discrete':
            self.box_insert_after('num_colors', 'otsu_thd')
            #self.otsu_thd.constant = False
        elif self.mode == 'Continuous':
            self.box_pop('otsu_thd')
        elif self.mode == 'ColorMap':
            self.num_colors = 2
            #todo adjust add/ remove color widgets methods
        self.param.trigger('num_colors')

    def _calc_colors(self, y_vals):
        if self.num_colors == 1:
            colors = np.full(len(y_vals), fill_value=self.colors[0], dtype='U7')
            colors[np.isnan(y_vals)] = np.nan
        elif self.mode == 'Discrete':
            full_thds = [-np.inf] + self.values[::-1] + [np.inf]
            colors = np.full(len(y_vals), fill_value=np.nan, dtype='U7')
            for lower, upper, color in zip(full_thds[:-1], full_thds[1:], self.colors[::-1]):
                b = (y_vals > lower) & (y_vals <= upper)
                colors[b] = color
        elif self.mode == 'Continuous':
            func = np.log if self.log_space else lambda x: x
            vals_space = (func(self.values))  # values in log space depending on setting
            norm = plt.Normalize(vals_space[-1], vals_space[0], clip=True)
            nodes = norm(vals_space[::-1])
            cmap = mpl.colors.LinearSegmentedColormap.from_list("custom_cmap", list(zip(nodes, self.colors[::-1])))

            try:
                colors_rgba = cmap(norm(func(y_vals)), bytes=True, alpha=0)
                colors = rgb_to_hex(colors_rgba)

                colors[np.isnan(y_vals)] = np.nan

            except ValueError as err:
                self.parent.logger.warning(err)
                return

        return colors

    @param.depends('values', 'colors', 'target', 'quantity', watch=True)
    def _get_colors(self):
        # todo or?
        if np.all(self.values == 0):
            return
        elif np.any(np.diff(self.values) > 0):  # Skip applying colors when not strictly monotonic descending
            return
        elif not self.target:
            return

        y_vals = self.parent.sources[self.target][self.quantity]  # full array including nan entries
        colors = self._calc_colors(y_vals)

        if colors is not None:
            self.parent.sources[self.target].source.data['color'] = colors  # this triggers an update of the graph

    @param.depends('num_colors', watch=True)
    def _update_num_colors(self):
        while len(self.colors_widgets) != self.num_colors:
            if len(self.colors_widgets) > self.num_colors:
                self._remove_color()
            elif len(self.colors_widgets) < self.num_colors:
                self._add_color()
        self.param.trigger('colors')

    @param.depends('num_colors', watch=True)
    def _update_num_values(self):
        diff = 1 if self.mode == 'Discrete' else 0
        while len(self.values_widgets) != self.num_colors - diff:
            if len(self.values_widgets) > self.num_colors - diff:
                self._remove_value()
            elif len(self.values_widgets) < self.num_colors - diff:
                self._add_value()

        self._update_bounds()
        self.param.trigger('values')

    def _add_value(self):
        try:
            first_value = self.values_widgets[-1].value
        except IndexError:
            first_value = 0

        default = float(first_value - 1)
        self.values.append(default)

        name = 'Threshold {}'.format(len(self.values_widgets) + 1)
        widget = pn.widgets.FloatInput(name=name, value=default)
        self.values_widgets.append(widget)
        i = len(self.values_widgets) + self.box_index('show_thds')
        self._box.insert(i, widget)
        widget.param.watch(self._value_event, ['value'])

    def _remove_value(self):
        widget = self.values_widgets.pop(-1)
        self.box_pop(widget)
        self.values.pop()

        [widget.param.unwatch(watcher) for watcher in widget.param._watchers]
        del widget

    def _add_color(self):
        try:
            default = DEFAULT_CLASS_COLORS[len(self.colors_widgets)]
        except IndexError:
            default = "#"+''.join(np.random.choice(list('0123456789abcdef'), 6))

        self.colors.append(default)
        widget = pn.widgets.ColorPicker(value=default)
        self.colors_widgets.append(widget)
        i = len(self.values_widgets) + len(self.colors_widgets) + self.box_index('show_thds')
        self._box.insert(i, widget)
        widget.param.watch(self._color_event, ['value'])

    def _remove_color(self):
        widget = self.colors_widgets.pop(-1)
        self.colors.pop()
        self.box_pop(widget)
        [widget.param.unwatch(watcher) for watcher in widget.param._watchers]
        del widget

    def _color_event(self, *events):
        for event in events:
            idx = list(self.colors_widgets).index(event.obj)
            self.colors[idx] = event.new


        #todo callback?
        self.param.trigger('colors')

    def _value_event(self, *events):
        """triggers when a single value gets changed"""
        for event in events:
            idx = list(self.values_widgets).index(event.obj)
            self.values[idx] = event.new

        self._update_bounds()
        self.param.trigger('values')

    def _update_bounds(self):
        for i, widget in enumerate(self.values_widgets):
            if i > 0:
                prev_value = float(self.values_widgets[i - 1].value)
                widget.end = np.nextafter(prev_value, prev_value - 1)
            else:
                widget.end = None

            if i < len(self.values_widgets) - 1:
                next_value = float(self.values_widgets[i + 1].value)
                widget.start = np.nextafter(next_value, next_value + 1)
            else:
                widget.start = None


class ColoringControl(ClassificationControl):
    # WIP class, skip tests


    def make_dict(self):
        widgets_dict = super().make_dict()
        widgets_dict.pop('quantity')

        return widgets_dict

    @param.depends('values', 'colors', 'target', 'quantity', watch=True)
    def _get_colors(self):
        # todo this part is repeated
        if np.all(self.values == 0):
            return
        elif np.any(np.diff(self.values) > 0):  # Skip applying colors when not strictly monotonic descending
            return
        elif not self.target:
            return
        elif 'scores_image' not in self.parent.sources.keys():
            return

        tgt_source = self.parent.sources[self.target] # full array including nan entries
        r_number = tgt_source.source.data['r_number']
        assert np.all(np.diff(r_number) == 1)


        headers = [f for f in tgt_source.source.data.keys() if f.replace('.', '', 1).isdigit()]

        headers.sort(key=float)
        timepoints = np.array([float(f) for f in headers])
        N_interpolate = 500
        interp_timepoints = np.linspace(0, timepoints.max(), num=N_interpolate, endpoint=True)
        data_array = np.stack([tgt_source.source.data[k] for k in headers])

        array = np.stack([np.interp(interp_timepoints, timepoints, data) for data in data_array.T]).T


        colors_hex = self._calc_colors(array.flatten())  # colors are in hex format
        if colors_hex is None:  # this is the colors not between 0 and 1 bug / error
            return

        print(colors_hex)
        colors_hex[colors_hex == 'nan'] = '#8c8c8c'
        colors_rgba = np.array([hex_to_rgba(h) for h in colors_hex])

        shape = (N_interpolate, len(r_number))
        img = np.empty(shape, dtype=np.uint32)
        view = img.view(dtype=np.uint8).reshape(*shape, 4)
        view[:] = colors_rgba.reshape(*shape, 4)

        img_source = self.parent.sources['scores_image']
        img_source.render_kwargs['dw'] = r_number.max()
        img_source.render_kwargs['dh'] = timepoints.max()
        img_source.source.data.update(img=[img], scores=[array])

        print('howdoe')

        #self.parent.sources[self.target].source.data['color'] = colors


class FileExportControl(ControlPanel):
    # todo check if docstring is true
    """
    This controller allows users to export and download datasets.

    All datasets can be exported as .txt tables.
    'Mappable' datasets (with r_number column) can be exported as .pml pymol script, which colors protein structures
    based on their 'color' column.

    """

    header = "File Export"
    target = param.Selector(label='Target dataset', doc='Name of the dataset to export')
    #todo add color param an dlink with protein viewer color

    def __init__(self, parent, **param):
        self.export_linear_download = pn.widgets.FileDownload(filename='<no data>', callback=self.linear_export_callback)
        self.pml_script_download = pn.widgets.FileDownload(filename='<no data>', callback=self.pml_export_callback)
        super(FileExportControl, self).__init__(parent, **param)

        self.parent.param.watch(self._sources_updated, ['sources'])
        try:  # todo write function that does the try/excepting + warnings (or also mixins?)
            self.parent.param.watch(self._series_updated, ['series'])
        except ValueError:
            pass

    def make_list(self):
        self.widget_dict.update(export_linear_download=self.export_linear_download, pml_script_download=self.pml_script_download)
        return super(FileExportControl, self).make_list()

    def _sources_updated(self, *events):
        objects = list(self.parent.sources.keys())
        self.param['target'].objects = objects

        if not self.target and objects:
            self.target = objects[0]

    def _series_updated(self, *events):
        self.c_term = int(self.parent.series.coverage.protein.c_term)

    def _make_pml(self, target):
        assert 'r_number' in self.export_dict.keys(), "Target export data must have 'r_number' column"

        try:
            #todo add no coverage field and link to the other no coverage field
            no_coverage = self.parent.control_panels['ProteinViewControl'].no_coverage
        except KeyError:
            no_coverage = '#8c8c8c'
            self.parent.logger.warning('No coverage color found, using default grey')

        try:
            c_term = self.parent.series.c_term
        except AttributeError:
            c_term = None

        try:
            script = colors_to_pymol(self.export_dict['r_number'], self.export_dict['color'],
                                     c_term=c_term, no_coverage=no_coverage)
            return script
        except KeyError:
            return None

    @property
    def export_dict(self):
        return {k: v for k, v in self.export_data_source.source.data.items() if not k.startswith('__')}

    @property
    def export_data_source(self):
        return self.parent.sources[self.target]

    @pn.depends('target', watch=True)
    def _update_filename(self):
        #todo subclass and split
        self.export_linear_download.filename = self.parent.series.state + '_' + self.target + '_linear.txt'
        if 'mapping' in self.export_data_source.tags:
            self.pml_script_download.filename = self.parent.series.state + '_' + self.target + '_pymol.pml'
            # self.pml_script_download.disabled = False
        else:
            self.pml_script_download.filename = 'Not Available'
            # self.pml_script_download.disabled = True # Enable/disable currently bugged:

    @pn.depends('target')
    def pml_export_callback(self):
        if self.target:
            io = StringIO()
            io.write('# ' + VERSION_STRING + ' \n')
            script = self._make_pml(self.target)
            try:
                io.write(script)
                io.seek(0)
                return io
            except TypeError:
                return None
        else:
            return None

    @pn.depends('target')  # param.depends?
    def linear_export_callback(self):
        io = StringIO()
        io.write('# ' + VERSION_STRING + ' \n')

        if self.target:
            self.export_data_source.export_df.to_csv(io, index=False)
            io.seek(0)
            return io
        else:
            return None


class DifferenceFileExportControl(FileExportControl):
    """
    This controller allows users to export and download datasets.

    'Mappable' datasets (with r_number column) can be exported as .pml pymol script, which colors protein structures
    based on their 'color' column.

    """

    accepted_tags = ['mapping']
    #todo include comparison info (x vs y) in output

    def _sources_updated(self, *events):  #refactor _parent_sources_updated on classificationcontrol
        data_sources = [k for k, src in self.parent.sources.items() if src.resolve_tags(self.accepted_tags)]
        self.param['target'].objects = list(data_sources)

        # Set target if its not set already
        if not self.target and data_sources:
            self.target = data_sources[-1]

    @pn.depends('target', watch=True)
    def _update_filename(self):
        self.export_linear_download.filename = self.target + '_linear.txt'
        if 'r_number' in self.export_dict.keys():
            self.pml_script_download.filename = self.target + '_pymol.pml'


class ProteinViewControl(ControlPanel):
    """
    This controller allows users control the Protein view figure.
    Structures can be specified either by RCSB ID or uploading a .pdb file.

    Colors are assigned according to 'color' column of the selected dataset.
    """

    header = 'Protein Viewer'
    accepted_tags = ['mapping']

    target_dataset = param.Selector(doc='Name of the dataset to apply coloring from')
    input_option = param.Selector(default='RCSB PDB', objects=['RCSB PDB', 'Upload File'],
                                  doc='Choose wheter to upload .pdb file or directly download from RCSB PDB.')
    rcsb_id = param.String(doc='RCSB PDB identifier of protein entry to download and visualize.')
    #load_structure = param.Action(lambda self: self._load_structure())
    no_coverage = param.Color(default='#8c8c8c', doc='Color to use for regions of no coverage.')
    representation = param.Selector(default='cartoon',
                                    objects=['backbone', 'ball+stick', 'cartoon', 'hyperball', 'licorice',
                                             'ribbon', 'rope', 'spacefill', 'surface'],
                                    doc='Representation to use to render the protein.')
    spin = param.Boolean(default=False, doc='Rotate the protein around an axis.')

    def __init__(self, parent, **params):
        self.file_input = pn.widgets.FileInput(accept='.pdb')
        super(ProteinViewControl, self).__init__(parent, **params)

        self.parent.param.watch(self._parent_sources_updated, ['sources'])
        self.input_option = 'RCSB PDB'

    def make_list(self):
        lst = super().make_list()
        lst.pop(2)  # Remove RCSB ID input field?
        lst.insert(2, self.file_input)  # add File input widget
        return lst

    def _parent_sources_updated(self, *events):
        #todo  this line repeats, put in base class
        data_sources = [k for k, src in self.parent.sources.items() if src.resolve_tags(self.accepted_tags)]
        self.param['target_dataset'].objects = data_sources
        if not self.target_dataset and data_sources:
            self.target_dataset = data_sources[0]

    @param.depends('input_option', watch=True)
    def _update_input_option(self):
        if self.input_option == 'Upload File':
            self.box_pop('rcsb_id')
            self.box_insert_after('input_option', self.file_input)
        elif self.input_option == 'RCSB PDB':
            self.box_pop(self.file_input)
            self.box_insert_after('input_option', 'rcsb_id')

        elif self.input_option == 'RCSB PDB':
            self.ngl_html.rcsb_id = self.rcsb_id


class OptionsControl(ControlPanel):
    """The controller is used for various settings."""

    header = 'Options'

    #todo this should be a component (mixin?) for apps who dont have these figures
    link_xrange = param.Boolean(True, doc='Link the X range of the coverage figure and other linear mapping figures.', constant=False)
    log_level = param.Selector(default='DEBUG', objects=['DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL', 'OFF', 'TRACE'],
                               doc='Set the logging level.')

    def __init__(self, parent, **param):
        super(OptionsControl, self).__init__(parent, **param)

    @property
    def enabled(self):
        return self.master_figure is not None and self.client_figures is not None

    @param.depends('link_xrange', watch=True)
    def _update_link(self):
        if self.enabled:
            if self.link_xrange:
                self._link()
            else:
                self._unlink()

    @property
    def client_figures(self):
        client_names = ['RateFigure', 'PFactFigure']
        return [self.parent.figure_panels[name].figure for name in client_names]

    @property
    def master_figure(self):
        return self.parent.figure_panels['CoverageFigure'].figure

    @property
    def figures(self):
        return [self.master_figure] + self.client_figures

    def _unlink(self):
        for fig in self.figures:
            fig.x_range.js_property_callbacks.pop('change:start')
            fig.x_range.js_property_callbacks.pop('change:end')

    def _link(self):
        for client in self.client_figures:
            self.master_figure.x_range.js_link('start',  client.x_range, 'start')
            self.master_figure.x_range.js_link('end', client.x_range, 'end')

            client.x_range.js_link('start', self.master_figure.x_range, 'start')
            client.x_range.js_link('end', self.master_figure.x_range, 'end')


class DeveloperControl(ControlPanel):
    """Controller with debugging options"""

    header = 'Developer Options'
    test_logging = param.Action(lambda self: self._action_test_logging())
    breakpoint_btn = param.Action(lambda self: self._action_break())
    test_btn = param.Action(lambda self: self._action_test())
    trigger_btn = param.Action(lambda self: self._action_trigger())
    print_btn = param.Action(lambda self: self._action_print())

    def __init__(self, parent, **params):
        super(DeveloperControl, self).__init__(parent, **params)

    def _action_test_logging(self):
        self.parent.logger.debug('TEST DEBUG MESSAGE')
        for i in range(20):
            self.parent.logger.info('dit is een test123')

    def _action_print(self):
        print(self.parent.doc)

    def _action_break(self):
        main_ctrl = self.parent
        control_panels = main_ctrl.control_panels
        figure_panels = main_ctrl.figure_panels
        sources = main_ctrl.sources

        print('Time for a break')

    def _action_test(self):
        from pathlib import Path
        src_file = r'C:\Users\jhsmi\pp\PyHDX\tests\test_data\ecSecB_torch_fit.txt'
        array = txt_to_np(src_file)
        data_dict = {name: array[name] for name in array.dtype.names}

        data_dict['color'] = np.full_like(array, fill_value=DEFAULT_COLORS['pfact'], dtype='<U7')
        data_source = DataSource(data_dict, x='r_number', tags=['mapping', 'pfact', 'deltaG'],
                                 renderer='circle', size=10, name='global_fit')

        self.parent.publish_data('global_fit', data_source)


    def _action_trigger(self):
        deltaG_figure = self.parent.figure_panels['DeltaGFigure']
        deltaG_figure.bk_pane.param.trigger('object')
