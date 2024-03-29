import logging
import re
import sys
import time
from datetime import datetime as dt
from datetime import timedelta as tdelta

import serial
import numpy as np
import astropy.units as u

def movingaverage(interval, window_size):
    """ A simple moving average function """
    window = np.ones(int(window_size)) / float(window_size)
    return np.convolve(interval, window, 'same')


# -----------------------------------------------------------------------------
# AAG Cloud Sensor Class
# -----------------------------------------------------------------------------
class AAGCloudSensor(object):
    """
    This class is for the AAG Cloud Sensor device which can be communicated with
    via serial commands.

    http://www.aagware.eu/aag/cloudwatcherNetwork/TechInfo/Rs232_Comms_v100.pdf
    http://www.aagware.eu/aag/cloudwatcherNetwork/TechInfo/Rs232_Comms_v110.pdf
    http://www.aagware.eu/aag/cloudwatcherNetwork/TechInfo/Rs232_Comms_v120.pdf

    Command List (from Rs232_Comms_v100.pdf)
    !A = Get internal name (recieves 2 blocks)
    !B = Get firmware version (recieves 2 blocks)
    !C = Get values (recieves 5 blocks)
         Zener voltage, Ambient Temperature, Ambient Temperature, Rain Sensor Temperature, HSB
    !D = Get internal errors (recieves 5 blocks)
    !E = Get rain frequency (recieves 2 blocks)
    !F = Get switch status (recieves 2 blocks)
    !G = Set switch open (recieves 2 blocks)
    !H = Set switch closed (recieves 2 blocks)
    !Pxxxx = Set PWM value to xxxx (recieves 2 blocks)
    !Q = Get PWM value (recieves 2 blocks)
    !S = Get sky IR temperature (recieves 2 blocks)
    !T = Get sensor temperature (recieves 2 blocks)
    !z = Reset RS232 buffer pointers (recieves 1 blocks)
    !K = Get serial number (recieves 2 blocks)

    Return Codes
    '1 '    Infra red temperature in hundredth of degree Celsius
    '2 '    Infra red sensor temperature in hundredth of degree Celsius
    '3 '    Analog0 output 0-1023 => 0 to full voltage (Ambient Temp NTC)
    '4 '    Analog2 output 0-1023 => 0 to full voltage (LDR ambient light)
    '5 '    Analog3 output 0-1023 => 0 to full voltage (Rain Sensor Temp NTC)
    '6 '    Analog3 output 0-1023 => 0 to full voltage (Zener Voltage reference)
    'E1'    Number of internal errors reading infra red sensor: 1st address byte
    'E2'    Number of internal errors reading infra red sensor: command byte
    'E3'    Number of internal errors reading infra red sensor: 2nd address byte
    'E4'    Number of internal errors reading infra red sensor: PEC byte NB: the error
            counters are reset after being read.
    'N '    Internal Name
    'V '    Firmware Version number
    'Q '    PWM duty cycle
    'R '    Rain frequency counter
    'X '    Switch Opened
    'Y '    Switch Closed

    Advice from the manual:

    * When communicating with the device send one command at a time and wait for
    the respective reply, checking that the correct number of characters has
    been received.

    * Perform more than one single reading (say, 5) and apply a statistical
    analysis to the values to exclude any outlier.

    * The rain frequency measurement is the one that takes more time - 280 ms

    * The following reading cycle takes just less than 3 seconds to perform:
        * Perform 5 times:
            * get IR temperature
            * get Ambient temperature
            * get Values
            * get Rain Frequency
        * get PWM value
        * get IR errors
        * get SWITCH Status

    """

    def __init__(self, serial_address=None):
        self.log = get_logger(self)
        self.log.setLevel(logging.INFO)

        self.log.debug('Using serial address: {}'.format(serial_address))
        if serial_address:
            self.log.info('Connecting to AAG Cloud Sensor')
            try:
                self.AAG = serial.Serial(serial_address, 9600, timeout=2)
                self.log.info("  Connected to Cloud Sensor on {}".format(serial_address))
            except OSError as e:
                self.log.error('Unable to connect to AAG Cloud Sensor')
                self.log.error('  {}'.format(e.errno))
                self.log.error('  {}'.format(e.strerror))
                self.AAG = None
            except:
                self.log.error("Unable to connect to AAG Cloud Sensor")
                self.AAG = None
        else:
            self.AAG = None

        # Initialize Values
        self.last_update = None
        self.safe = None
        self.ambient_temp = None
        self.sky_temp = None
        self.wind_speed = None
        self.internal_voltage = None
        self.LDR_resistance = None
        self.rain_sensor_temp = None
        self.PWM = None
        self.errors = None
        self.switch = None
        self.safe_dict = None
        self.hibernate = 0.500  # time to wait after failed query

        # Set Up Heater
        if 'heater' in self.cfg:
            self.heater_cfg = self.cfg['heater']
        else:
            self.heater_cfg = {
                'low_temp': 0,
                'low_delta': 6,
                'high_temp': 20,
                'high_delta': 4,
                'min_power': 10,
                'impulse_temp': 10,
                'impulse_duration': 60,
                'impulse_cycle': 600,
            }
        self.heater_PID = PID(Kp=3.0, Ki=0.02, Kd=200.0,
                              max_age=300,
                              output_limits=[self.heater_cfg['min_power'], 100])

        self.impulse_heating = None
        self.impulse_start = None

        # Command Translation
        self.commands = {'!A': 'Get internal name',
                         '!B': 'Get firmware version',
                         '!C': 'Get values',
                         '!D': 'Get internal errors',
                         '!E': 'Get rain frequency',
                         '!F': 'Get switch status',
                         '!G': 'Set switch open',
                         '!H': 'Set switch closed',
                         'P\d\d\d\d!': 'Set PWM value',
                         '!Q': 'Get PWM value',
                         '!S': 'Get sky IR temperature',
                         '!T': 'Get sensor temperature',
                         '!z': 'Reset RS232 buffer pointers',
                         '!K': 'Get serial number',
                         'v!': 'Query if anemometer enabled',
                         'V!': 'Get wind speed',
                         'M!': 'Get electrical constants',
                         '!Pxxxx': 'Set PWM value to xxxx',
                         }
        self.expects = {'!A': '!N\s+(\w+)!',
                        '!B': '!V\s+([\d\.\-]+)!',
                        '!C': '!6\s+([\d\.\-]+)!4\s+([\d\.\-]+)!5\s+([\d\.\-]+)!',
                        '!D': '!E1\s+([\d\.]+)!E2\s+([\d\.]+)!E3\s+([\d\.]+)!E4\s+([\d\.]+)!',
                        '!E': '!R\s+([\d\.\-]+)!',
                        '!F': '!Y\s+([\d\.\-]+)!',
                        'P\d\d\d\d!': '!Q\s+([\d\.\-]+)!',
                        '!Q': '!Q\s+([\d\.\-]+)!',
                        '!S': '!1\s+([\d\.\-]+)!',
                        '!T': '!2\s+([\d\.\-]+)!',
                        '!K': '!K(\d+)\s*\\x00!',
                        'v!': '!v\s+([\d\.\-]+)!',
                        'V!': '!w\s+([\d\.\-]+)!',
                        'M!': '!M(.{12})',
                        }
        self.delays = {
            '!E': 0.350,
            'P\d\d\d\d!': 0.750,
        }

        if self.AAG:
            # Query Device Name
            result = self.query('!A')
            if result:
                self.name = result[0].strip()
                self.log.info('  Device Name is "{}"'.format(self.name))
            else:
                self.name = ''
                self.log.warning('  Failed to get Device Name')
                sys.exit(1)

            # Query Firmware Version
            result = self.query('!B')
            if result:
                self.firmware_version = result[0].strip()
                self.log.info('  Firmware Version = {}'.format(self.firmware_version))
            else:
                self.firmware_version = ''
                self.log.warning('  Failed to get Firmware Version')
                sys.exit(1)

            # Query Serial Number
            result = self.query('!K')
            if result:
                self.serial_number = result[0].strip()
                self.log.info('  Serial Number: {}'.format(self.serial_number))
            else:
                self.serial_number = ''
                self.log.warning('  Failed to get Serial Number')
                sys.exit(1)

    def get_reading(self):
        """ Calls commands to be performed each time through the loop """
        weather_data = dict()

        if self.db is None:
            self.db = PanMongo()
            self.log.info('Connected to PanMongo')
        else:
            weather_data = self.update_weather()
            self.calculate_and_set_PWM()

        return weather_data

    def send(self, send, delay=0.100):

        found_command = False
        for cmd in self.commands.keys():
            if re.match(cmd, send):
                self.log.debug('Sending command: {}'.format(self.commands[cmd]))
                found_command = True
                break
        if not found_command:
            self.log.warning('Unknown command: "{}"'.format(send))
            return None

        self.log.debug('  Clearing buffer')
        cleared = self.AAG.read(self.AAG.inWaiting())
        if len(cleared) > 0:
            self.log.debug('  Cleared: "{}"'.format(cleared.decode('utf-8')))

        self.AAG.write(send.encode('utf-8'))
        time.sleep(delay)
        response = self.AAG.read(self.AAG.inWaiting()).decode('utf-8')
        self.log.debug('  Response: "{}"'.format(response))
        ResponseMatch = re.match('(!.*)\\x11\s{12}0', response)
        if ResponseMatch:
            result = ResponseMatch.group(1)
        else:
            result = response

        return result

    def query(self, send, maxtries=5):
        found_command = False
        for cmd in self.commands.keys():
            if re.match(cmd, send):
                self.log.debug('Sending command: {}'.format(self.commands[cmd]))
                found_command = True
                break
        if not found_command:
            self.log.warning('Unknown command: "{}"'.format(send))
            return None

        if cmd in self.delays.keys():
            self.log.debug('  Waiting delay time of {:.3f} s'.format(self.delays[cmd]))
            delay = self.delays[cmd]
        else:
            delay = 0.200
        expect = self.expects[cmd]
        count = 0
        result = None
        while not result and (count <= maxtries):
            count += 1
            result = self.send(send, delay=delay)

            MatchExpect = re.match(expect, result)
            if not MatchExpect:
                self.log.debug('Did not find {} in response "{}"'.format(expect, result))
                result = None
                time.sleep(self.hibernate)
            else:
                self.log.debug('Found {} in response "{}"'.format(expect, result))
                result = MatchExpect.groups()
        return result

    def get_ambient_temperature(self, n=5):
        """
        Populates the self.ambient_temp property

        Calculation is taken from Rs232_Comms_v100.pdf section "Converting values
        sent by the device to meaningful units" item 5.
        """
        self.log.debug('Getting ambient temperature')
        values = []

        for i in range(0, n):
            try:
                value = float(self.query('!T')[0])
                ambient_temp = value / 100.

            except:
                pass
            else:
                self.log.debug('  Ambient Temperature Query = {:.1f}\t{:.1f}'.format(value, ambient_temp))
                values.append(ambient_temp)

        if len(values) >= n - 1:
            self.ambient_temp = np.median(values) * u.Celsius
            self.log.debug('  Ambient Temperature = {:.1f}'.format(self.ambient_temp))
        else:
            self.ambient_temp = None
            self.log.debug('  Failed to Read Ambient Temperature')

        return self.ambient_temp

    def get_sky_temperature(self, n=9):
        """
        Populates the self.sky_temp property

        Calculation is taken from Rs232_Comms_v100.pdf section "Converting values
        sent by the device to meaningful units" item 1.

        Does this n times as recommended by the "Communication operational
        recommendations" section in Rs232_Comms_v100.pdf
        """
        self.log.debug('Getting sky temperature')
        values = []
        for i in range(0, n):
            try:
                value = float(self.query('!S')[0]) / 100.
            except:
                pass
            else:
                self.log.debug('  Sky Temperature Query = {:.1f}'.format(value))
                values.append(value)
        if len(values) >= n - 1:
            self.sky_temp = np.median(values) * u.Celsius
            self.log.debug('  Sky Temperature = {:.1f}'.format(self.sky_temp))
        else:
            self.sky_temp = None
            self.log.debug('  Failed to Read Sky Temperature')
        return self.sky_temp

    def get_values(self, n=5):
        """
        Populates the self.internal_voltage, self.LDR_resistance, and
        self.rain_sensor_temp properties

        Calculation is taken from Rs232_Comms_v100.pdf section "Converting values
        sent by the device to meaningful units" items 4, 6, 7.
        """
        self.log.debug('Getting "values"')
        ZenerConstant = 3
        LDRPullupResistance = 56.
        RainPullUpResistance = 1
        RainResAt25 = 1
        RainBeta = 3450.
        ABSZERO = 273.15
        internal_voltages = []
        LDR_resistances = []
        rain_sensor_temps = []
        for i in range(0, n):
            responses = self.query('!C')
            try:
                internal_voltage = 1023 * ZenerConstant / float(responses[0])
                internal_voltages.append(internal_voltage)
                LDR_resistance = LDRPullupResistance / ((1023. / float(responses[1])) - 1.)
                LDR_resistances.append(LDR_resistance)
                r = np.log((RainPullUpResistance / ((1023. / float(responses[2])) - 1.)) / RainResAt25)
                rain_sensor_temp = 1. / ((r / RainBeta) + (1. / (ABSZERO + 25.))) - ABSZERO
                rain_sensor_temps.append(rain_sensor_temp)
            except:
                pass

        # Median Results
        if len(internal_voltages) >= n - 1:
            self.internal_voltage = np.median(internal_voltages) * u.volt
            self.log.debug('  Internal Voltage = {:.2f}'.format(self.internal_voltage))
        else:
            self.internal_voltage = None
            self.log.debug('  Failed to read Internal Voltage')

        if len(LDR_resistances) >= n - 1:
            self.LDR_resistance = np.median(LDR_resistances) * u.kohm
            self.log.debug('  LDR Resistance = {:.0f}'.format(self.LDR_resistance))
        else:
            self.LDR_resistance = None
            self.log.debug('  Failed to read LDR Resistance')

        if len(rain_sensor_temps) >= n - 1:
            self.rain_sensor_temp = np.median(rain_sensor_temps) * u.Celsius
            self.log.debug('  Rain Sensor Temp = {:.1f}'.format(self.rain_sensor_temp))
        else:
            self.rain_sensor_temp = None
            self.log.debug('  Failed to read Rain Sensor Temp')

        return (self.internal_voltage, self.LDR_resistance, self.rain_sensor_temp)

    def get_rain_frequency(self, n=5):
        """
        Populates the self.rain_frequency property
        """
        self.log.debug('Getting rain frequency')
        values = []
        for i in range(0, n):
            try:
                value = float(self.query('!E')[0])
                self.log.debug('  Rain Freq Query = {:.1f}'.format(value))
                values.append(value)
            except:
                pass
        if len(values) >= n - 1:
            self.rain_frequency = np.median(values)
            self.log.debug('  Rain Frequency = {:.1f}'.format(self.rain_frequency))
        else:
            self.rain_frequency = None
            self.log.debug('  Failed to read Rain Frequency')
        return self.rain_frequency

    def get_PWM(self):
        """
        Populates the self.PWM property.

        Calculation is taken from Rs232_Comms_v100.pdf section "Converting values
        sent by the device to meaningful units" item 3.
        """
        self.log.debug('Getting PWM value')
        try:
            value = self.query('!Q')[0]
            self.PWM = float(value) * 100. / 1023.
            self.log.debug('  PWM Value = {:.1f}'.format(self.PWM))
        except:
            self.PWM = None
            self.log.debug('  Failed to read PWM Value')
        return self.PWM

    def set_PWM(self, percent, ntries=15):
        """
        """
        count = 0
        success = False
        if percent < 0.:
            percent = 0.
        if percent > 100.:
            percent = 100.
        while not success and count <= ntries:
            self.log.info('Setting PWM value to {:.1f} %'.format(percent))
            send_digital = int(1023. * float(percent) / 100.)
            send_string = 'P{:04d}!'.format(send_digital)
            result = self.query(send_string)
            count += 1
            if result:
                self.PWM = float(result[0]) * 100. / 1023.
                if abs(self.PWM - percent) > 5.0:
                    self.log.warning('  Failed to set PWM value!')
                    time.sleep(2)
                else:
                    success = True
                self.log.debug('  PWM Value = {:.1f}'.format(self.PWM))

    def get_errors(self):
        """
        Populates the self.IR_errors property
        """
        self.log.debug('Getting errors')
        response = self.query('!D')
        if response:
            self.errors = {'error_1': str(int(response[0])),
                           'error_2': str(int(response[1])),
                           'error_3': str(int(response[2])),
                           'error_4': str(int(response[3]))}
            self.log.debug("  Internal Errors: {} {} {} {}".format(
                self.errors['error_1'],
                self.errors['error_2'],
                self.errors['error_3'],
                self.errors['error_4'],
            ))

        else:
            self.errors = {'error_1': None,
                           'error_2': None,
                           'error_3': None,
                           'error_4': None}
        return self.errors

    def get_switch(self, maxtries=3):
        """
        Populates the self.switch property

        Unlike other queries, this method has to check if the return matches a
        !X or !Y pattern (indicating open and closed respectively) rather than
        read a value.
        """
        self.log.debug('Getting switch status')
        self.switch = None
        tries = 0
        status = None
        while not status:
            tries += 1
            response = self.send('!F')
            if re.match('!Y            1!', response):
                status = 'OPEN'
            elif re.match('!X            1!', response):
                status = 'CLOSED'
            else:
                status = None
            if not status and tries >= maxtries:
                status = 'UNKNOWN'
        self.switch = status
        self.log.debug('  Switch Status = {}'.format(self.switch))
        return self.switch

    def wind_speed_enabled(self):
        """
        Method returns true or false depending on whether the device supports
        wind speed measurements.
        """
        self.log.debug('Checking if wind speed is enabled')
        try:
            enabled = bool(self.query('v!')[0])
            if enabled:
                self.log.debug('  Anemometer enabled')
            else:
                self.log.debug('  Anemometer not enabled')
        except:
            enabled = None
        return enabled

    def get_wind_speed(self, n=3):
        """
        Populates the self.wind_speed property

        Based on the information in Rs232_Comms_v120.pdf document

        Medians n measurements.  This isn't mentioned specifically by the manual
        but I'm guessing it won't hurt.
        """
        self.log.debug('Getting wind speed')
        if self.wind_speed_enabled():
            values = []
            for i in range(0, n):
                result = self.query('V!')
                if result:
                    value = float(result[0])
                    self.log.debug('  Wind Speed Query = {:.1f}'.format(value))
                    values.append(value)
            if len(values) >= 3:
                self.wind_speed = np.median(values) * u.km / u.hr
                self.log.debug('  Wind speed = {:.1f}'.format(self.wind_speed))
            else:
                self.wind_speed = None
        else:
            self.wind_speed = None
        return self.wind_speed

    def capture(self, update_mongo=True):
        """ Query the CloudWatcher """
        if update_mongo and self.db is None:
            self.db = PanMongo()
            self.log.info('Connected to PanMongo')

        self.log.debug("Updating weather")

        data = {}
        data['weather_sensor_name'] = self.name
        data['weather_sensor_firmware_version'] = self.firmware_version
        data['weather_sensor_serial_number'] = self.serial_number

        if self.get_sky_temperature():
            data['sky_temp_C'] = self.sky_temp.value
        if self.get_ambient_temperature():
            data['ambient_temp_C'] = self.ambient_temp.value
        self.get_values()
        if self.internal_voltage:
            data['internal_voltage_V'] = self.internal_voltage.value
        if self.LDR_resistance:
            data['ldr_resistance_Ohm'] = self.LDR_resistance.value
        if self.rain_sensor_temp:
            data['rain_sensor_temp_C'] = "{:.02f}".format(self.rain_sensor_temp.value)
        if self.get_rain_frequency():
            data['rain_frequency'] = self.rain_frequency
        if self.get_PWM():
            data['pwm_value'] = self.PWM
        if self.get_errors():
            data['errors'] = self.errors
        if self.get_wind_speed():
            data['wind_speed_KPH'] = self.wind_speed.value

        # Make Safety Decision
        self.safe_dict = self.make_safety_decision(data)

        data['safe'] = self.safe_dict['Safe']
        data['sky_condition'] = self.safe_dict['Sky']
        data['wind_condition'] = self.safe_dict['Wind']
        data['gust_condition'] = self.safe_dict['Gust']
        data['rain_condition'] = self.safe_dict['Rain']

        self.calculate_and_set_PWM()

        if update_mongo:
            self.db.insert_current('weather', data)

        return data

    def AAG_heater_algorithm(self, target, last_entry):
        """
        Uses the algorithm described in RainSensorHeaterAlgorithm.pdf to
        determine PWM value.

        Values are for the default read cycle of 10 seconds.
        """
        deltaT = last_entry['rain_sensor_temp_C'] - target
        scaling = 0.5
        if deltaT > 8.:
            deltaPWM = -40 * scaling
        elif deltaT > 4.:
            deltaPWM = -20 * scaling
        elif deltaT > 3.:
            deltaPWM = -10 * scaling
        elif deltaT > 2.:
            deltaPWM = -6 * scaling
        elif deltaT > 1.:
            deltaPWM = -4 * scaling
        elif deltaT > 0.5:
            deltaPWM = -2 * scaling
        elif deltaT > 0.3:
            deltaPWM = -1 * scaling
        elif deltaT < -0.3:
            deltaPWM = 1 * scaling
        elif deltaT < -0.5:
            deltaPWM = 2 * scaling
        elif deltaT < -1.:
            deltaPWM = 4 * scaling
        elif deltaT < -2.:
            deltaPWM = 6 * scaling
        elif deltaT < -3.:
            deltaPWM = 10 * scaling
        elif deltaT < -4.:
            deltaPWM = 20 * scaling
        elif deltaT < -8.:
            deltaPWM = 40 * scaling
        return int(deltaPWM)

    def calculate_and_set_PWM(self):
        """
        Uses the algorithm described in RainSensorHeaterAlgorithm.pdf to decide
        whether to use impulse heating mode, then determines the correct PWM
        value.
        """
        self.log.debug('Calculating new PWM Value')
        # Get Last n minutes of rain history
        now = dt.utcnow()
        start = now - tdelta(0, int(self.heater_cfg['impulse_cycle']))

        entries = [x for x in self.db.weather.find({'date': {'$gt': start, '$lt': now}})]

        self.log.debug('  Found {} entries in last {:d} seconds.'.format(
            len(entries), int(self.heater_cfg['impulse_cycle']), ))

        last_entry = [x for x in self.db.current.find({"type": "weather"})][0]['data']
        rain_history = [x['data']['rain_safe']
                        for x
                        in entries
                        if 'rain_safe' in x['data'].keys()
                        ]

        if 'ambient_temp_C' not in last_entry.keys():
            self.log.warning('  Do not have Ambient Temperature measurement.  Can not determine PWM value.')
        elif 'rain_sensor_temp_C' not in last_entry.keys():
            self.log.warning('  Do not have Rain Sensor Temperature measurement.  Can not determine PWM value.')
        else:
            # Decide whether to use the impulse heating mechanism
            if len(rain_history) > 3 and not np.any(rain_history):
                self.log.debug('  Consistent wet/rain in history.  Using impulse heating.')
                if self.impulse_heating:
                    impulse_time = (now - self.impulse_start).total_seconds()
                    if impulse_time > float(self.heater_cfg['impulse_duration']):
                        self.log.debug('  Impulse heating has been on for > {:.0f} seconds.  Turning off.'.format(
                            float(self.heater_cfg['impulse_duration'])
                        ))
                        self.impulse_heating = False
                        self.impulse_start = None
                    else:
                        self.log.debug('  Impulse heating has been on for {:.0f} seconds.'.format(
                            impulse_time))
                else:
                    self.log.debug('  Starting impulse heating sequence.')
                    self.impulse_start = now
                    self.impulse_heating = True
            else:
                self.log.debug('  No impulse heating needed.')
                self.impulse_heating = False
                self.impulse_start = None

            # Set PWM Based on Impulse Method or Normal Method
            if self.impulse_heating:
                target_temp = float(last_entry['ambient_temp_C']) + float(self.heater_cfg['impulse_temp'])
                if last_entry['rain_sensor_temp_C'] < target_temp:
                    self.log.debug('  Rain sensor temp < target.  Setting heater to 100 %.')
                    self.set_PWM(100)
                else:
                    new_PWM = self.AAG_heater_algorithm(target_temp, last_entry)
                    self.log.debug('  Rain sensor temp > target.  Setting heater to {:d} %.'.format(new_PWM))
                    self.set_PWM(new_PWM)
            else:
                if last_entry['ambient_temp_C'] < self.heater_cfg['low_temp']:
                    deltaT = self.heater_cfg['low_delta']
                elif last_entry['ambient_temp_C'] > self.heater_cfg['high_temp']:
                    deltaT = self.heater_cfg['high_delta']
                else:
                    frac = (last_entry['ambient_temp_C'] - self.heater_cfg['low_temp']) /\
                           (self.heater_cfg['high_temp'] - self.heater_cfg['low_temp'])
                    deltaT = self.heater_cfg['low_delta'] + frac * \
                        (self.heater_cfg['high_delta'] - self.heater_cfg['low_delta'])
                target_temp = last_entry['ambient_temp_C'] + deltaT
                new_PWM = int(self.heater_PID.recalculate(float(last_entry['rain_sensor_temp_C']),
                                                          new_set_point=target_temp))
                self.log.debug('  last PID interval = {:.1f} s'.format(self.heater_PID.last_interval))
                self.log.debug('  target={:4.1f}, actual={:4.1f}, new PWM={:3.0f}, P={:+3.0f}, I={:+3.0f} ({:2d}), D={:+3.0f}'.format(
                    target_temp, float(last_entry['rain_sensor_temp_C']),
                    new_PWM, self.heater_PID.Kp * self.heater_PID.Pval,
                    self.heater_PID.Ki * self.heater_PID.Ival,
                    len(self.heater_PID.history),
                    self.heater_PID.Kd * self.heater_PID.Dval,
                ))
                self.set_PWM(new_PWM)

    def make_safety_decision(self, current_values):
        """
        Method makes decision whether conditions are safe or unsafe.
        """
        self.log.debug('Making safety decision')
        threshold_cloudy = self.cfg.get('threshold_cloudy', -22.5)
        threshold_very_cloudy = self.cfg.get('threshold_very_cloudy', -15.)
        threshold_windy = self.cfg.get('threshold_windy', 20.)
        threshold_very_windy = self.cfg.get('threshold_very_windy', 30)
        threshold_gusty = self.cfg.get('threshold_gusty', 40.)
        threshold_very_gusty = self.cfg.get('threshold_very_gusty', 50.)
        threshold_wet = self.cfg.get('threshold_wet', 2000.)
        threshold_rain = self.cfg.get('threshold_rainy', 1700.)
        safety_delay = self.cfg.get('safety_delay', 15.)
        end = dt.utcnow()
        start = end - tdelta(0, int(safety_delay * 60))

        if self.db is None:
            self.db = PanMongo()
            self.log.info('Connected to PanMongo')

        entries = [x for x in self.db.weather.find({'date':\
                   {'$gt': start, '$lt': end}}).sort([('date', pymongo.ASCENDING)])]
        self.log.debug('Found {} weather entries in last {:.0f} minutes'.format(
                          len(entries), safety_delay))

        # Cloudiness
        sky_diff = [x['data']['sky_temp_C'] - x['data']['ambient_temp_C']
                    for x in entries
                    if ('ambient_temp_C' and 'sky_temp_C') in x['data'].keys()]

        if len(sky_diff) == 0:
            self.log.debug('  UNSAFE: no sky temperatures found')
            sky_safe = False
            cloud_condition = 'Unknown'
        else:
            if max(sky_diff) > threshold_very_cloudy:
                self.log.debug('UNSAFE: Very cloudy. Max sky diff {:.1f} C'.format(
                                  safety_delay, max(sky_diff)))
                sky_safe = False
            else:
                sky_safe = True

            last_cloud = current_values['sky_temp_C'] - current_values['ambient_temp_C']
            if last_cloud > threshold_very_cloudy:
                cloud_condition = 'Very Cloudy'
            elif last_cloud > threshold_cloudy:
                cloud_condition = 'Cloudy'
            else:
                cloud_condition = 'Clear'
            self.log.debug('Cloud Condition: {} (Sky-Amb={:.1f} C)'.format(cloud_condition, sky_diff[-1]))

        # Wind (average and gusts)
        wind_speed = [x['data']['wind_speed_KPH']
                      for x in entries
                      if 'wind_speed_KPH' in x['data'].keys()]

        if len(wind_speed) == 0:
            self.log.debug('  UNSAFE: no wind speed readings found')
            wind_safe = False
            gust_safe = False
            wind_condition = 'Unknown'
            gust_condition = 'Unknown'
        else:
            typical_data_interval = (end - min([x['date'] for x in entries])).total_seconds() / len(entries)
            mavg_count = int(np.ceil(120. / typical_data_interval))
            wind_mavg = movingaverage(wind_speed, mavg_count)

            # Windy?
            if max(wind_mavg) > threshold_very_windy:
                self.log.debug('  UNSAFE:  Very windy in last {:.0f} min. Max wind speed {:.1f} kph'.format(
                    safety_delay, max(wind_mavg)))
                wind_safe = False
            else:
                wind_safe = True

            if wind_mavg[-1] > threshold_very_windy:
                wind_condition = 'Very Windy'
            elif wind_mavg[-1] > threshold_windy:
                wind_condition = 'Windy'
            else:
                wind_condition = 'Calm'
            self.log.debug('  Wind Condition: {} ({:.1f} km/h)'.format(wind_condition, wind_mavg[-1]))

            # Gusty?
            if max(wind_speed) > threshold_very_gusty:
                self.log.debug('  UNSAFE:  Very gusty in last {:.0f} min. Max gust speed {:.1f} kph'.format(
                    safety_delay, max(wind_speed)))
                gust_safe = False
            else:
                gust_safe = True

            current_wind = current_values['wind_speed_KPH']
            if current_wind > threshold_very_gusty:
                gust_condition = 'Very Gusty'
            elif current_wind > threshold_gusty:
                gust_condition = 'Gusty'
            else:
                gust_condition = 'Calm'

            self.log.debug('  Gust Condition: {} ({:.1f} km/h)'.format(gust_condition, wind_speed[-1]))

        # Rain
        rf_value = [x['data']['rain_frequency'] for x in entries if 'rain_frequency' in x['data'].keys()]

        if len(rf_value) == 0:
            rain_safe = False
            rain_condition = 'Unknown'
        else:
            # Check current values
            if current_values['rain_frequency'] <= threshold_rain:
                rain_condition = 'Rain'
                rain_safe = False
            elif current_values['rain_frequency'] <= threshold_wet:
                rain_condition = 'Wet'
                rain_safe = False
            else:
                rain_condition = 'Dry'
                rain_safe = True

            # If safe now, check last 15 minutes
            if rain_safe:
                if min(rf_value) <= threshold_rain:
                    self.log.debug('  UNSAFE:  Rain in last {:.0f} min.'.format(safety_delay))
                    rain_safe = False
                elif min(rf_value) <= threshold_wet:
                    self.log.debug('  UNSAFE:  Wet in last {:.0f} min.'.format(safety_delay))
                    rain_safe = False
                else:
                    rain_safe = True

            self.log.debug('  Rain Condition: {}'.format(rain_condition))

        safe = sky_safe & wind_safe & gust_safe & rain_safe
        translator = {True: 'safe', False: 'unsafe'}
        self.log.debug('Weather is {}'.format(translator[safe]))

        ## Reload config
        self.config = load_config()

        return {'Safe': safe,
                'Sky': cloud_condition,
                'Wind': wind_condition,
                'Gust': gust_condition,
                'Rain': rain_condition}
