
from pathlib import Path

import panel as pn
import yaml

from pyhdx import VERSION_STRING
from pyhdx.web.constructor import AppConstructor
from pyhdx.web.log import logger


@logger('pyhdx')
def main_app():
    cwd = Path(__file__).parent.resolve()
    yaml_dict = yaml.safe_load((cwd / 'pyhdx_app.yaml').read_text(encoding='utf-8'))

    ctr = AppConstructor(loggers={'pyhdx': main_app.logger})

    ctrl = ctr.parse(yaml_dict)

    ctrl.start()

    fmt = {'accent_base_color': '#1d417a'}

    tmpl = pn.template.FastGridTemplate(title=f'{VERSION_STRING}', **fmt)
    controllers = ctrl.control_panels.values()
    controls = pn.Accordion(*[controller.panel for controller in controllers], toggle=True)
    tmpl.sidebar.append(controls)

    views_names = [
        'rfu_scatter',
        'coverage',
        'logging_info',
        'logging_debug',
        'protein',
        'ddG_overlay',
        'rates',
        'gibbs_overlay',
        'peptide_mse',
        #'peptide_scatter',
        'peptide_overlay',
        'loss_lines'
        ]

    views = {v: ctrl.views[v] for v in views_names}
    [v.update() for v in views.values()]

    cov_tab = pn.Tabs(
        ('Coverage', views['coverage'].panel),
        ('Protein', views['protein'].panel),
        ('Peptide MSE', views['peptide_mse'].panel)
    )

    scatter_tab = pn.Tabs(
        ('RFU', views['rfu_scatter'].panel),
        ('Rates', views['rates'].panel),
        ('ΔG', views['gibbs_overlay'].panel),
        ('ΔΔG', views['ddG_overlay'].panel),
    )

    log_tab = pn.Tabs(
        ('Info log', views['logging_info'].panel),
        ('Debug log', views['logging_debug'].panel)
    )

    peptide_tab = pn.Tabs(
        ('Peptide', views['peptide_overlay'].panel),
        ('Losses', views['loss_lines'].panel)
    )

    tmpl.main[0:3, 0:6] = cov_tab
    tmpl.main[0:3, 6:12] = scatter_tab
    tmpl.main[3:5, 0:6] = log_tab
    tmpl.main[3:5, 6:12] = peptide_tab

    return ctrl, tmpl