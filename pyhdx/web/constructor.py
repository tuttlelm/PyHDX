import collections
import logging

from distributed import Client

from pyhdx.local_cluster import default_client
from pyhdx.support import gen_subclasses
from pyhdx.web.controllers import *
from pyhdx.web.filters import *
from pyhdx.web.main_controllers import PyHDXController, MainController
from pyhdx.web.opts import OptsBase
from pyhdx.web.sources import *
from pyhdx.web.views import AppViewBase


class AppConstructor(param.Parameterized):

    sources = param.Dict(default={})

    filters = param.Dict(default={})

    opts = param.Dict(default={})

    views = param.Dict(default={})

    controllers = param.Dict(default={}) #?

    ctrl_class = param.ClassSelector(class_=MainController, instantiate=False)

    logger = param.ClassSelector(default=None, class_=logging.Logger, doc="Logger object")

    client = param.ClassSelector(default=None, class_=Client)

    def __init__(self, **params):
        super().__init__(**params)

        self.classes = self.find_classes()
       # self._logger_counters = {}

    def parse(self, yaml_dict, **kwargs):
        self._parse_sections(yaml_dict)
        for name, dic in yaml_dict['modules'].items():
            self._parse_sections(dic)

        d = yaml_dict['controllers']
        self.controllers = {name: self._resolve_class(name, 'controller') for name in d}

        main_ctrl = yaml_dict['main_controller']
        _type = main_ctrl.pop('type')
        main_ctrl_class = self._resolve_class(_type, 'main')
        ctrl = main_ctrl_class(
            self.controllers.values(),
            sources=self.sources,
            filters=self.filters,
            opts=self.opts,
            views=self.views,
            logger=self.logger,
            client=default_client(),
            **kwargs, **main_ctrl
        )

        return ctrl
    #
    # def make_ctrl(self, **kwargs):
    #     ctrl = PyHDXController(  # todo ctrl_class from yaml
    #         self.controllers.values(),
    #         sources=self.sources,
    #         filters=self.filters,
    #         opts=self.opts,
    #         views=self.views,
    #         logger=self.logger,
    #         client=default_client(),
    #         **kwargs  # todo yaml these
    #     )

        # return ctrl

    @staticmethod
    def find_classes():
        base_classes = {
            'main': MainController,
            'filter': AppFilterBase,
            'source': AppSourceBase,
            'view': AppViewBase,
            'opt': OptsBase,
            'controller': ControlPanel}
        classes = {}
        for key, cls in base_classes.items():
            base_cls = base_classes[key]
            all_classes = list([cls for cls in gen_subclasses(base_cls) if hasattr(cls, '_type')]) # or check for None on _type
            types = [cls._type for cls in all_classes]
            if len(types) != len(set(types)):
                print([item for item, count in collections.Counter(types).items() if count > 1])
                raise ValueError
            class_dict = {cls._type: cls for cls in all_classes}
            classes[key] = class_dict

        return classes

    def _parse_sections(self, yaml_dict):
        sections = ['sources', 'filters', 'opts', 'views']
        for section in sections:
            func = getattr(self, f'add_{section[:-1]}')  # Remove trailing s to get correct adder function
            d = yaml_dict.get(section, {})
            for name, spec in d.items():
                # todo move to classmethod on object which checks spec/kwargs  (also prevents logger from needing a source)
                if 'type' not in spec:
                    raise KeyError(f"The field 'type' is not specified for {section[:-1]} {name!r}")
                _type = spec.pop('type')
                if section in ['filters', 'views'] and 'source' not in spec:
                    raise KeyError(f"The field 'source' is not specified for {section[:-1]} {name!r}")
                func(name, _type, **spec)

    def add_filter(self, name, _type, **kwargs):
        kwargs = self._resolve_kwargs(**kwargs)
        class_ = self._resolve_class(_type, 'filter')
        obj = class_(name=name, **kwargs)
        self.filters[name] = obj

    def add_tool(self, name, _type, **kwargs):
        pass

    def add_opt(self, name, _type, **kwargs):
        class_ = self._resolve_class(_type, 'opt')
        obj = class_(name=name, **kwargs)
        self.opts[name] = obj

    def add_source(self, name, _type, **kwargs):
        class_ = self._resolve_class(_type, 'source')
        obj = class_(name=name, **kwargs)

        self.sources[name] = obj

    def add_view(self, name, _type, **kwargs):
        kwargs = self._resolve_kwargs(**kwargs)
        class_ = self._resolve_class(_type, 'view')
        obj = class_(name=name, **kwargs)
        self.views[name] = obj

    # def get_logger(self, name):
    #     # todo add app name
    #     count = self._logger_counters.get(name, 0)
    #
    #     dt = datetime.datetime.now().strftime('%Y%m%d')
    #     logger = logging.getLogger(f'{name}.{dt}_{count}')
    #     logger.setLevel(logging.DEBUG)
    #     sys.stderr = StreamToLogger(logger, logging.DEBUG)
    #
    #     self._logger_counters[name] = count + 1
    #     wrapper.logger = logger

    def _resolve_class(self, _type, cls):
        return self.classes[cls][_type]

    def _resolve_kwargs(self, **kwargs):
        resolved = {}
        for k, v in kwargs.items():
            if k == 'source':
                # temporary:
                if v is None:
                    resolved[k] = v
                else:
                    obj = self.sources.get(v, None) or self.filters.get(v)
                    resolved[k] = obj
            elif k == 'opts':
                v = [v] if isinstance(v, str) else v  # allow singly opt by str
                resolved[k] = [self.opts[vi] for vi in v]
            elif k == 'dependencies':  # dependencies are opts/filters/controllers? (anything with .updated event)
                all_objects = []
                for type_, obj_list in v.items():
                    for obj in obj_list:
                        all_objects.append(getattr(self, type_)[obj])
                resolved[k] = all_objects
            elif k == 'logger':
                # v : something v
                resolved[k] = self.logger

            else:
                resolved[k] = v

        return resolved



