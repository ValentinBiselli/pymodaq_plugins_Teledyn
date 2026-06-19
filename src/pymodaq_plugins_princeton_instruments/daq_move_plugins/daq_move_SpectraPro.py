# daq_move_SpectraPro.py
# PyMoDAQ 5 actuator plugin for Princeton Instruments SpectraPro SP-2500i
# Communication via RS-232 serial (pyserial)

from pymodaq.control_modules.move_utility_classes import (DAQ_Move_base, comon_parameters_fun,
                                                          main, DataActuatorType, DataActuator)
from pymodaq_utils.utils import ThreadCommand
from pymodaq_gui.parameter import Parameter

import serial
import time


class DAQ_Move_SpectraPro(DAQ_Move_base):
    """
    PyMoDAQ 5 actuator plugin for the Princeton Instruments SpectraPro SP-2500i monochromator.
    Communication via RS-232 serial using pyserial.

    Tested with:
        - SpectraPro SP-2500i
        - PyMoDAQ 5
        - Windows 10/11

    Serial settings: 9600 baud, 8N1, XON/XOFF flow control
    """

    is_multiaxes = False
    _axis_names = ['Wavelength']
    _controller_units = 'nm'
    _epsilon = 0.005  # 0.1 nm tolerance
    data_actuator_type = DataActuatorType.DataActuator

    params = [
        {'title': 'com_port', 'name': 'com_port', 'type': 'str', 'value': 'COM6'},
        {'title': 'Grating', 'name': 'grating', 'type': 'list', 'value': 1, 'min': 1, 'max': 9, 'readonly': False},
    ] + comon_parameters_fun(is_multiaxes, axis_names=_axis_names, epsilon=_epsilon)

    def ini_attributes(self):
        """Initialize instance attributes for the actuator"""
        self.controller: serial.Serial = None

    def ini_stage(self, controller=None):
        """Initialize the SpectraPro serial communication.

        Parameters
        ----------
        controller: object, optional
            Existing controller object (slave mode). None for master mode.

        Returns
        -------
        info: str
            Initialization status message
        initialized: bool
            True if successful, False otherwise
        """
        if self.is_master:
            try:
                com_port = self.settings.child('com_port').value()

                self.controller = serial.Serial(
                    port=com_port,
                    baudrate=9600,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    xonxoff=True,
                    timeout=5
                )
                print(f"DEBUG Opened serial port {com_port} for SpectraPro")
                grating_list = self._query_gratings()
                self.settings.child('grating').setLimits(grating_list)
                print(f"DEBUG Grating list initialized: {grating_list}")
                initialized = True
                info = "SpectraPro serial port opened successfully"

            except Exception as e:
                initialized = False
                info = f"Failed to initialize SpectraPro: {type(e).__name__}: {str(e)}"
        else:
            self.controller = controller
            initialized = True
            info = "Using external controller"

        return info, initialized
    
    def get_actuator_value(self):
        """Query the current wavelength from the SpectraPro.

        Returns
        -------
        DataActuator: Current wavelength position in nm, with scaling applied.
        """
        self.controller.write(b'?NM\r')
        response = self.controller.read_until(b'ok').decode(errors='ignore')

        wavelength_nm = None
        for token in response.split():
            try:
                wavelength_nm = float(token)
                break
            except ValueError:
                continue

        if wavelength_nm is None:
            raise ValueError(f"Could not parse wavelength from response: {response!r}")

        pos = DataActuator(data=wavelength_nm, units=self.axis_unit)
        pos = self.get_position_with_scaling(pos)

        return pos
    
    def move_abs(self, value: DataActuator):
        """Move to absolute target wavelength.

        Parameters
        ----------
        value: DataActuator
            Target wavelength in nm
        """
        value = self.check_bound(value)
        self.target_value = value
        value = self.set_position_with_scaling(value)

        target_nm = value.value(self.axis_unit)

        command = f"{target_nm:.1f} >GOTO\r".encode()
        self.controller.write(command)
        response = self.controller.read_until(b'ok').decode(errors='ignore')

        info = f"Moved to {target_nm:.1f} nm"
        self.emit_status(ThreadCommand('Update_Status', [info]))

    def close(self):
            """Terminate the serial communication."""
            if self.is_master:
                self.controller.close()
            self.controller = None
    
    def stop_motion(self):
        """Stop the current wavelength move in progress."""
        self.controller.write(b'MONO-STOP\r')
        response = self.controller.read_until(b'ok').decode(errors='ignore')

        info = "Stopped wavelength motion"
        self.emit_status(ThreadCommand('Update_Status', [info]))

    def _query_gratings(self):
        """Query the SpectraPro for installed gratings and parse the response.

        Returns
        -------
        list of str: Display strings like "1: 600 g/mm BLZ= 1.0UM" for each
            installed grating slot (slots reported as "Not Installed" are skipped).
        """
        self.controller.write(b'?gratings\r')
        response = self.controller.read_until(b'ok').decode(errors='ignore')
        response = response.replace('\x1a', '')

        grating_list = []
        for line in response.splitlines():
            line = ' '.join(line.split())
            if not line or 'ok' in line.lower() or 'gratings' in line.lower():
                continue
            if 'not installed' in line.lower():
                continue

            slot_number = line.split()[0]
            description = line[len(slot_number):].strip()
            grating_list.append(f"{slot_number}: {description}")

        return grating_list
    
    def commit_settings(self, param: Parameter):
        """Apply the consequences of a change of value in the actuator settings.

        Parameters
        ----------
        param: Parameter
            A given parameter (within detector_settings) whose value has been changed by the user
        """
        if param.name() == 'grating':
            selected = param.value()
            slot_number = selected.split(':')[0].strip()

            command = f"{slot_number} grating\r".encode()

            original_timeout = self.controller.timeout
            self.controller.timeout = 25

            self.controller.write(command)
            response = self.controller.read_until(b'ok').decode(errors='ignore')

            self.controller.timeout = original_timeout

            info = f"Changed to grating {selected}"
            self.emit_status(ThreadCommand('Update_Status', [info]))