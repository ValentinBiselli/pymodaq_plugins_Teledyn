import numpy as np
from pymodaq_utils.utils import ThreadCommand
from pymodaq_utils.logger import set_logger, get_module_name
from pymodaq_gui.parameter import Parameter
try:
    from pymodaq_gui.plotting.items.roi import RoiInfo  # pymodaq > 5.1.x
except ImportError:
    from pymodaq_gui.plotting.utils.plot_utils import RoiInfo

from pymodaq.utils.data import DataFromPlugins, Axis
from pymodaq.control_modules.viewer_utility_classes import DAQ_Viewer_base, comon_parameters, main

from qtpy import QtWidgets, QtCore

from ...hardware.picam_utils import define_pymodaq_pyqt_parameter, sort_by_priority_list, remove_settings_from_list

import pylablib as pll
pll.par['devices/dlls/picam'] = r'C:\Program Files\Common Files\Princeton Instruments\Picam\Runtime'
import pylablib.devices.PrincetonInstruments as PI


class DAQ_2DViewer_picam(DAQ_Viewer_base):
    """
    Base class for Princeton Instruments CCD camera controlled with the picam c library.
    """

    params = comon_parameters + [
        {'title': 'Controller ID:', 'name': 'controller_id', 'type': 'str', 'value': '', 'readonly': True},
        {'title': 'Serial number:', 'name': 'serial_number', 'type': 'list', 'limits': []},
        {'title': 'Simple Settings', 'name': 'simple_settings', 'type': 'bool', 'value': True}
    ]

    callback_signal = QtCore.Signal()
    hardware_averaging = False

    def ini_attributes(self):
        """Initialize instance attributes for the detector"""
        self.controller = None
        self.x_axis = None
        self.y_axis = None
        self.data_shape = 'Data2D'
        self.callback_thread = None
        self.roi_select_info = None

    def __init__(self, parent=None, params_state=None):
        super().__init__(parent, params_state)

    def roi_select(self, roi_info: RoiInfo, ind_viewer: int = 0):
        """Automatically called when a user uses the RoiSelect ROI from a 2D viewer"""
        self.roi_select_info = roi_info
        self.roi_select_viewer_index = ind_viewer

    def _update_all_settings(self):
        for grandparam in ['settable_camera_parameters', 'read_only_camera_parameters']:
            for param in self.settings.child(grandparam).children():
                self.controller.get_attribute(param.title()).update_limits()
                newval = self.controller.get_attribute_value(param.title())
                if newval != param.value():
                    self.settings.child(grandparam, param.name()).setValue(newval)
                    self.emit_status(ThreadCommand('Update_Status', [f'updated {param.title()}: {param.value()}']))

    def _update_rois(self):
        new_x = self.settings['settable_camera_parameters', 'rois', 'x']
        new_width = self.settings['settable_camera_parameters', 'rois', 'width']
        new_xbinning = self.settings['settable_camera_parameters', 'rois', 'x_binning']
        new_y = self.settings['settable_camera_parameters', 'rois', 'y']
        new_height = self.settings['settable_camera_parameters', 'rois', 'height']
        new_ybinning = self.settings['settable_camera_parameters', 'rois', 'y_binning']

        new_roi = (new_x, new_width, new_xbinning, new_y, new_height, new_ybinning)
        if new_roi != tuple(self.controller.get_attribute_value('ROIs')[0]):
            self.controller.set_roi(new_x, new_x + new_width, new_y, new_y + new_height,
                                    hbin=new_xbinning, vbin=new_ybinning)
            self.emit_status(ThreadCommand('Update_Status', [f'Changed ROI: {new_roi}']))
            self._update_all_settings()
            self.controller.clear_acquisition()
            self.controller._commit_parameters()
            self.controller.setup_acquisition()
            self._prepare_view()

    def commit_settings(self, param: Parameter):
        """Apply the consequences of a change of value in the detector settings

        Parameters
        ----------
        param: Parameter
            A given parameter (within detector_settings) whose value has been changed by the user
        """
        if param.parent().name() == "rois":
            self._update_rois()
        elif self.controller.get_attribute(param.title()).writable:
            if self.controller.get_attribute_value(param.title()) != param.value():
                self.controller.set_attribute_value(param.title(), param.value(),
                                                    truncate=True, error_on_missing=True)
                self.emit_status(ThreadCommand('Update_Status', [f'Changed {param.title()}: {param.value()}']))
                self._update_all_settings()

    def emit_data(self):
        try:
            frame = self.controller.read_newest_image()
            axes = self._camera_axes(frame.shape)
            self.data_grabed_signal.emit([DataFromPlugins(name='Picam',
                                                          data=[np.squeeze(frame)],
                                                          dim=self.data_shape,
                                                          labels=[f'Picam_{self.data_shape}'],
                                                          **axes)])
            QtWidgets.QApplication.processEvents()
        except Exception as e:
            self.emit_status(ThreadCommand('Update_Status', [str(e), 'log']))

    def ini_detector(self, controller=None):
        """Detector communication initialization

        Parameters
        ----------
        controller: (object)
            custom object of a PyMoDAQ plugin (Slave case). None if only one actuator/detector by controller
            (Master case)

        Returns
        -------
        info: str
        initialized: bool
            False if initialization failed otherwise True
        """
        try:
            if self.settings.child('controller_status').value() == "Slave":
                if controller is None:
                    raise Exception('no controller has been defined externally while this detector is a slave one')
                else:
                    self.ini_detector_init(old_controller=controller, new_controller=controller)
            else:
                dvcs = PI.list_cameras()
                self.settings.child('serial_number').setLimits([dvc.serial_number for dvc in dvcs])
                camera = PI.PicamCamera(self.settings.child('serial_number').value())
                self.settings.child('controller_id').setValue(camera.get_device_info().model)
                self.ini_detector_init(old_controller=None, new_controller=camera)

            wait_func = lambda: self.controller.wait_for_frame(since='lastread', nframes=1, timeout=20.0)
            callback = PicamCallback(wait_func)

            self.callback_thread = QtCore.QThread()
            callback.moveToThread(self.callback_thread)
            callback.data_sig.connect(self.emit_data)
            self.callback_signal.connect(callback.wait_for_acquisition)
            self.callback_thread.callback = callback
            self.callback_thread.start()

            atd = self.controller.get_all_attributes(copy=True)
            camera_params = []
            for k, v in atd.items():
                tmp = define_pymodaq_pyqt_parameter(v)
                if tmp is not None:
                    camera_params.append(tmp)

            read_and_set_parameters = [par for par in camera_params if not par['readonly']]
            read_only_parameters = [par for par in camera_params if par['readonly']]

            # List of priority for ordering the parameters in the UI.
            priority = ['Exposure Time',
                        'ADC Speed',
                        'ADC Analog Gain',
                        'ADC Quality',
                        'ROIs',
                        'Sensor Temperature Set Point',
                        ]
            remove = ['Active Width',
                      'Active Height',
                      'Active Left Margin',
                      'Active Top Margin',
                      'Active Right Margin',
                      'Active Bottom Margin',
                      'Shutter Closing Delay',
                      'Shutter Opening Delay',
                      'Readout Count',
                      'ADC Bit Depth',
                      'Time Stamp Bit Depth',
                      'Frame Tracking Bit Depth',
                      'Shutter Delay Resolution',
                      'Shutter Timing Monde',
                      'Trigger Response',
                      'Trigger Determination',
                      'Output Signal',
                      'Pixel Format',
                      'Invert Output Signal',
                      'Disable Data Formatting',
                      'Track Frames',
                      'Clean Section Final Height',
                      'Clean Section Final Height Count',
                      'Clean Cycle Count',
                      'Clean Cycle Height',
                      'Clean Serial Register',
                      'Clean Until Trigger',
                      'Normalize Orientation',
                      'Correct Pixel Bias',
                      'Shutter Timing Mode',
                      'Time Stamps',
                      'Time Stamp Resolution',
                      ]
            read_and_set_parameters = sort_by_priority_list(read_and_set_parameters, priority)
            if self.settings.child('simple_settings').value():
                read_and_set_parameters = remove_settings_from_list(read_and_set_parameters, remove)

            # List of priority for ordering the parameters in the UI but for read only params, which is less
            # important (kindof)
            priority = ['Sensor Temperature',
                        'Readout Time Calculation',
                        'Frame Rate Calculation',
                        'Pixel Width',
                        'Pixel Height',
                        ]
            remove = ['Sensor Masked Height',
                      'Sensor Masked Top Margin',
                      'Sensor Masked Bottom Margin',
                      'Gap Width',
                      'Gap Height',
                      'CCD Characteristics',
                      'Exact Readout Count Maximum',
                      'Pixel Width',
                      'Pixel Height',
                      'Frame Size',
                      'Frame Stride',
                      'Pixel Bit Depth',
                      'Sensor Secondary Masked Height',
                      'Sensor Active Width',
                      'Sensor Active Height',
                      'Sensor Active Left Margin',
                      'Sensor Active Top Margin',
                      'Sensor Active Right Margin',
                      'Sensor Active Bottom Margin',
                      'Sensor Secondary Active Height',
                      'Sensor Active Extended Height',
                      'Sensor Temperature Status',
                      'Orientation',
                      'Readout Orientation',
                      'Sensor Type',
                      ]
            read_only_parameters = sort_by_priority_list(read_only_parameters, priority)
            if self.settings.child('simple_settings').value():
                read_only_parameters = remove_settings_from_list(read_only_parameters, remove)

            self.settings.addChild({'title': 'Settable Camera Parameters',
                                    'name': 'settable_camera_parameters',
                                    'type': 'group',
                                    'children': read_and_set_parameters})
            self.settings.addChild({'title': 'Read Only Camera Parameters',
                                    'name': 'read_only_camera_parameters',
                                    'type': 'group',
                                    'children': read_only_parameters})

            self._prepare_view()

            info = "Initialised camera"
            initialized = True
            return info, initialized

        except Exception as e:
            self.emit_status(ThreadCommand('Update_Status', [str(e), 'log']))
            info = str(e)
            initialized = False
            return info, initialized

    def close(self):
        """
        Terminate the communication protocol
        """
        self.controller.close()
        self.controller = None
        self.settings.child('settable_camera_parameters').clearChildren()
        self.settings.child('settable_camera_parameters').remove()
        self.settings.child('read_only_camera_parameters').clearChildren()
        self.settings.child('read_only_camera_parameters').remove()
        self.status.initialized = False
        self.status.controller = None
        self.status.info = ""

    def _toggle_non_online_parameters(self, enabled):
        for param in self.settings.child('settable_camera_parameters').children():
            if not self.controller.get_attribute(param.title()).can_set_online:
                param.setOpts(enabled=enabled)
        for param in self.settings.child('settable_camera_parameters', "rois").children():
            param.setOpts(enabled=enabled)

    def _prepare_view(self):
        """Preparing a data viewer by emitting temporary data. Typically, needs to be called whenever the
        ROIs are changed"""
        wx = self.settings.child('settable_camera_parameters', 'rois', 'width').value()
        wy = self.settings.child('settable_camera_parameters', 'rois', 'height').value()
        bx = self.settings.child('settable_camera_parameters', 'rois', 'x_binning').value()
        by = self.settings.child('settable_camera_parameters', 'rois', 'y_binning').value()

        sizex = wx // bx
        sizey = wy // by

        mock_data = np.zeros((sizey, sizex))

        if sizey != 1 and sizex != 1:
            data_shape = 'Data2D'
        else:
            data_shape = 'Data1D'

        if data_shape != self.data_shape:
            self.data_shape = data_shape
            axes = self._camera_axes(mock_data.shape)
            self.data_grabed_signal_temp.emit([DataFromPlugins(name='Picam',
                                                               data=[np.squeeze(mock_data)],
                                                               dim=self.data_shape,
                                                               labels=[f'Picam_{self.data_shape}'],
                                                               **axes)])
            QtWidgets.QApplication.processEvents()

    def _camera_axes(self, shape):
        axes = {}
        if len(shape) >= 2:
            axes['y_axis'] = Axis('yaxis', units='px', data=np.arange(shape[0]), index=0)
            axes['x_axis'] = Axis('xaxis', units='px', data=np.arange(shape[1]), index=1)
        elif len(shape) == 1:
            axes['x_axis'] = Axis('xaxis', units='px', data=np.arange(shape[0]), index=0)
        return axes

    def grab_data(self, Naverage=1, **kwargs):
        """
        Grabs the data. Synchronous method (kinda).
        ----------
        Naverage: (int) Number of averaging
        kwargs: (dict) of others optionals arguments
        """
        try:
            # Warning, acquisition_in_progress returns 1,0 and not a real bool
            if not self.controller.acquisition_in_progress():
                self._toggle_non_online_parameters(enabled=False)
                self.controller.clear_acquisition()
                self.controller.start_acquisition()
            self.callback_signal.emit()
        except Exception as e:
            self.emit_status(ThreadCommand('Update_Status', [str(e), "log"]))

    def callback(self):
        """optional asynchrone method called when the detector has finished its acquisition of data"""
        raise NotImplementedError

    def stop(self):
        """Stop the acquisition."""
        self.controller.stop_acquisition()
        self.controller.clear_acquisition()
        self._toggle_non_online_parameters(enabled=True)
        return ''


class PicamCallback(QtCore.QObject):
    data_sig = QtCore.Signal()

    def __init__(self, wait_fn):
        super().__init__()
        self.wait_fn = wait_fn

    def wait_for_acquisition(self):
        new_data = self.wait_fn()
        if new_data is not False:
            self.data_sig.emit()


if __name__ == '__main__':
    main(__file__)