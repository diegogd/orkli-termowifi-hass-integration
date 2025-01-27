"""Module that provides utility functions for handling temperature and humidity values.

and for printing formatted traces.
"""


def printTrace(trace):
    """Print trace in formatted way for debugging purposes."""
    # add an space every two bytes
    traces = [trace[i : i + 1].hex().upper() for i in range(len(trace))]

    formated_traces = ""
    for i, item in enumerate(traces):
        if i < 2:
            # foreground gray
            formated_traces += f"\033[91m{item}\033[0m "
        elif i < 4:
            formated_traces += f"\033[92m{item}\033[0m "
        elif i < 5:
            formated_traces += f"\033[93m{item}\033[0m "
        elif i < 6:
            formated_traces += f"\033[94m{item}\033[0m "
        else:
            formated_traces += f"\033[95m{item}\033[0m "
    print(formated_traces)


def temperature_from_value(value, offset=30):
    """Transform trace value to temperature.

    Data start in value 30, there are 40 graduations, so each graduation is 0.5º.
    Starts at 15ºC up to 35ºC.
    """
    base_value = value - offset
    return base_value * 0.5 + 15


def value_from_temperature(temperature, offset=30):
    """Transform temperature to trace value.

    Data start in value 30, there are 40 graduations, so each graduation is 0.5º.
    Starts at 15ºC up to 35ºC.
    """
    base_value = int((temperature - 15) / 0.5)
    return base_value + offset


def value_to_ambient(value):
    """Transform trace value to ambient temperature.

    Data start in value 71, there are 91 graduations, so each graduation is 0.5º.
    Starts at 45.5ºC down to -0.5ºC.
    """
    base = value - 71
    degrees = base * 0.5
    return 45.5 - degrees


def ambient_to_value(temperature):
    """Transform ambient temperature to trace value.

    Data start in value 71, there are 91 graduations, so each graduation is 0.5º.
    Starts at 45.5ºC down to -0.5ºC.
    """
    degrees = 45.5 + temperature
    base = degrees / 0.5
    return base + 71


def value_to_humidity(value):
    """Transform trace value to humidity percentage.

    Data is from 0 to 255, representing 0% to 100% humidity.
    """
    absolute_humidity = value * 100.0 / 255.0
    return int(absolute_humidity)
