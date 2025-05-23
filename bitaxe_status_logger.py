import requests
import time
import signal
import sys
import argparse
import re
from datetime import datetime
import os
import csv

# ANSI Color Codes
GREEN = "\033[32m"  # Darker green
ORANGE = "\033[95m"  # Orange
RED = "\033[91m"
RESET = "\033[0m"

# Configuration
CONFIG = {
    "run_duration": 600,  # seconds (10 minutes per run)
    "log_interval": 10,  # seconds (1 minute)
    "status_interval": 10,  # seconds
    "max_temp_warning": 62,  # °C for chip temperature
    "max_temp_critical": 65,  # °C for chip temperature
    "max_vrtemp_warning": 80,  # °C for voltage regulator temperature
    "max_vrtemp_critical": 85,  # °C for voltage regulator temperature
    "max_power_warning": 23,  # W for power
    "max_power_critical": 26,  # W for power
    "min_frequency": 400,  # MHz (safety minimum)
    "min_core_voltage": 1000,  # mV (safety minimum)
    "critical_advance_margin": 2,  # Margin below critical thresholds to advance to next settings
    "readings_to_advance": 5,  # Number of readings to take before allowing another settings adjustment
    "advance_delay": 7200,  # seconds (120 minutes) to wait before advancing after a critical fallback
}

# Global variables
system_info = {
    "frequency": None,
    "power": None,
    "voltage": None,
    "current": None,
    "temp": None,
    "vrTemp": None,
    "hashRate": None,
    "coreVoltage": None,
    "coreVoltageActual": None,
    "jth": None  # Joules per Terahash (J/TH)
}
global_min_values = {key: float('inf') for key in system_info}
global_max_values = {key: float('-inf') for key in system_info}
is_interrupted = False
critical_temp_reached = False
initial_frequency = None
initial_core_voltage = None
bitaxe_ip = None
readings_filename = None
summaries_filename = None
best_hashrate = 0.0
best_frequency = None
best_voltage = None
value_pairs = []  # List of (voltage, frequency) tuples from values.csv
last_fallback_time = None  # Timestamp of last critical fallback
last_fallback_voltage = None  # Voltage before the fallback

def signal_handler(sig, frame):
    global is_interrupted
    is_interrupted = True
    print(ORANGE + "\nStopping status logger..." + RESET)

signal.signal(signal.SIGINT, signal_handler)

def validate_ip(ip):
    """Basic IP address validation."""
    pattern = r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$"
    if not re.match(pattern, ip):
        raise ValueError("Invalid IP address format. Use format like 192.168.2.205")
    return f"http://{ip}"

def read_values_csv(filename):
    """Read and sort voltage-frequency pairs from CSV file."""
    global value_pairs
    try:
        with open(filename, 'r') as f:
            reader = csv.reader(f)
            next(reader, None)  # Skip header if present
            value_pairs = [(int(row[0]), int(row[1])) for row in reader if len(row) >= 2]
        # Sort by voltage (first column)
        value_pairs.sort(key=lambda x: x[0])
        if not value_pairs:
            raise ValueError("Values CSV file is empty or improperly formatted")
        print(GREEN + f"Loaded {len(value_pairs)} voltage-frequency pairs from {filename}" + RESET)
    except FileNotFoundError:
        raise FileNotFoundError(f"Values CSV file '{filename}' not found")
    except Exception as e:
        raise ValueError(f"Error reading values CSV file: {e}")

def parse_arguments():
    """Parse command-line arguments and print help if required parameters are missing."""
    parser = argparse.ArgumentParser(
        description="Bitaxe status logger for monitoring hashrate, temperature, and power across a frequency range or in monitor-only mode."
    )
    parser.add_argument(
        "-v", "--voltage",
        type=int,
        required=True,
        help="Core voltage in mV (minimum 1000 mV)"
    )
    parser.add_argument(
        "-f", "--frequency",
        type=int,
        required=True,
        help="Initial frequency in MHz (minimum 400 MHz)"
    )
    parser.add_argument(
        "-ip", "--ip_address",
        type=str,
        required=True,
        help="Bitaxe IP address (e.g., 192.168.2.205)"
    )
    parser.add_argument(
        "-range",
        type=int,
        default=10,
        help="Frequency range in MHz to test above and below the initial frequency (default 10 MHz, ignored in monitor mode)"
    )
    parser.add_argument(
        "-step",
        type=int,
        default=2,
        help="Frequency step size in MHz (default 2 MHz, ignored in monitor mode)"
    )
    parser.add_argument(
        "-reboot",
        type=int,
        default=None,
        help="Number of consecutive identical hashrate readings to trigger a reboot (optional)"
    )
    parser.add_argument(
        "-m", "--monitor",
        action="store_true",
        help="Run in monitor-only mode at the specified frequency indefinitely (sets range=0, step=0)"
    )
    parser.add_argument(
        "-values",
        type=str,
        help="Path to values.csv file with known good voltage and frequency pairs (used in monitor mode)"
    )

    args = parser.parse_args()

    if args.voltage < CONFIG["min_core_voltage"]:
        parser.error(f"Voltage must be at least {CONFIG['min_core_voltage']} mV")
    if args.frequency < CONFIG["min_frequency"]:
        parser.error(f"Frequency must be at least {CONFIG['min_frequency']} MHz")
    if args.range < 0:
        parser.error("Range must be non-negative")
    if args.step <= 0:
        parser.error("Step must be positive")
    if args.reboot is not None and args.reboot <= 0:
        parser.error("Reboot threshold must be positive")
    if args.values and not args.monitor:
        parser.error("The --values option is only valid in monitor mode (-m)")
    if args.values:
        read_values_csv(args.values)

    return args.voltage, args.frequency, validate_ip(args.ip_address), args.range, args.step, args.reboot, args.monitor, args.values

def fetch_system_info(run_min_values, run_max_values, run_sum_values, run_count_values, hashrate_readings):
    """Fetch system settings and update min/max/sum/count and hashrate readings."""
    try:
        response = requests.get(f"{bitaxe_ip}/api/system/info", timeout=10)
        response.raise_for_status()
        data = response.json()
        system_info["frequency"] = data.get("frequency", 550)
        system_info["power"] = data.get("power", 0)
        system_info["voltage"] = data.get("voltage", 0)
        system_info["current"] = data.get("current", 0)
        system_info["temp"] = data.get("temp", 0)
        system_info["vrTemp"] = data.get("vrTemp", 0)
        system_info["hashRate"] = data.get("hashRate", 0)
        system_info["coreVoltage"] = data.get("coreVoltage", 1250)
        system_info["coreVoltageActual"] = data.get("coreVoltageActual", 1250)
        system_info["jth"] = system_info["power"] / (system_info["hashRate"] / 1000) if system_info["hashRate"] > 0 else 0
        
        # Update run-specific and global min/max, and run-specific sum/count
        for key in system_info:
            run_min_values[key] = min(run_min_values[key], system_info[key])
            run_max_values[key] = max(run_max_values[key], system_info[key])
            global_min_values[key] = min(global_min_values[key], system_info[key])
            global_max_values[key] = max(global_max_values[key], system_info[key])
            run_sum_values[key] += system_info[key]
            run_count_values[key] += 1
        
        # Append hashrate to readings list for reboot tracking
        hashrate_readings.append(system_info["hashRate"])
        
        return True
    except requests.RequestException as e:
        print(RED + f"Error fetching system info: {e}" + RESET)
        return False

def set_system_settings(frequency, core_voltage):
    """Set Bitaxe frequency and core voltage."""
    frequency = max(CONFIG["min_frequency"], frequency)
    core_voltage = max(CONFIG["min_core_voltage"], core_voltage)
    try:
        # Use PATCH to /api/system with correct key "coreVoltage"
        payload = {"frequency": frequency, "coreVoltage": core_voltage}
        print(GREEN + f"Sending PATCH request to {bitaxe_ip}/api/system with payload: {payload}" + RESET)
        response = requests.patch(f"{bitaxe_ip}/api/system", json=payload, timeout=10)
        response.raise_for_status()
        print(GREEN + f"Set frequency to {frequency} MHz, core voltage to {core_voltage} mV" + RESET)
        
        # Verify settings
        time.sleep(5)  # Allow settings to stabilize
        try:
            response = requests.get(f"{bitaxe_ip}/api/system/info", timeout=10)
            response.raise_for_status()
            data = response.json()
            actual_freq = data.get("frequency", 0)
            actual_volt = data.get("coreVoltage", 0)
            print(GREEN + f"Verified settings: Actual frequency {actual_freq} MHz, actual core voltage {actual_volt} mV" + RESET)
            if abs(actual_freq - frequency) > 1 or abs(actual_volt - core_voltage) > 1:
                print(RED + f"Error: Settings did not apply correctly. Requested: {frequency} MHz, {core_voltage} mV; "
                            f"Actual: {actual_freq} MHz, {actual_volt} mV" + RESET)
                return False
        except requests.RequestException as e:
            print(RED + f"Could not verify settings: {e}" + RESET)
            return False
        
        return True
    except requests.RequestException as e:
        print(RED + f"Error setting system settings (PATCH /api/system): {e}" + RESET)
        return False

def reboot_bitaxe():
    """Reboot the Bitaxe using the API."""
    try:
        response = requests.post(f"{bitaxe_ip}/api/system/restart", timeout=10)
        response.raise_for_status()
        print(GREEN + "Bitaxe rebooted successfully." + RESET)
        return True
    except requests.RequestException as e:
        print(RED + f"Error rebooting Bitaxe: {e}" + RESET)
        return False

def log_data(frequency, core_voltage, run_number, note="", min_values=None, max_values=None, sum_values=None, count_values=None):
    """Log system data to readings file or summaries to summaries file."""
    global readings_filename, summaries_filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Log time-series data to readings file
    if not min_values and not max_values:
        try:
            with open(readings_filename, "a") as f:
                if os.path.getsize(readings_filename) == 0:
                    f.write("Timestamp,Hashrate(GH/s),Frequency(MHz),Temp(°C),VRTemp(°C),CoreVoltage(mV),CoreVoltageActual(mV),"
                            "Power(W),Current(mA),Voltage(mV),J/TH,Note\n")
                f.write(f"{timestamp},{system_info['hashRate']:.2f},{system_info['frequency']},"
                        f"{system_info['temp']:.2f},{system_info['vrTemp']:.2f},{system_info['coreVoltage']},"
                        f"{system_info['coreVoltageActual']},{system_info['power']:.2f},{system_info['current']:.2f},"
                        f"{system_info['voltage']:.2f},{system_info['jth']:.2f},{note}\n")
            return readings_filename
        except IOError as e:
            print(RED + f"Error logging readings data: {e}" + RESET)
            return readings_filename
    
    # Log summaries to summaries file (skipped in monitor mode)
    try:
        with open(summaries_filename, "a") as f:
            avg_hashrate = sum_values["hashRate"] / count_values["hashRate"] if count_values["hashRate"] > 0 else 0
            f.write(f"\nRun {run_number} Summary: Frequency {frequency} MHz, Voltage {core_voltage} mV, Avg Hashrate {avg_hashrate:.2f} GH/s\n")
            f.write("Metric,Min,Max,Avg\n")
            for key in min_values:
                avg = sum_values[key] / count_values[key] if count_values[key] > 0 else 0
                unit = ' MHz' if key == 'frequency' else ' W' if key == 'power' else '°C' if key in ['temp', 'vrTemp'] else ' GH/s' if key == 'hashRate' else ' J/TH' if key == 'jth' else ' mV' if 'Voltage' in key else ' mA'
                f.write(f"{key},{min_values[key]:.2f}{unit},{max_values[key]:.2f}{unit},{avg:.2f}{unit}\n")
            f.write("\n")
        return summaries_filename
    except IOError as e:
        print(RED + f"Error logging summaries data: {e}" + RESET)
        return summaries_filename

def display_status(reading_count, total_readings, run_number, total_tests, start_time, monitor_mode=False, min_values=None, max_values=None, sum_values=None, count_values=None):
    """Display current system status, including coreVoltage, reading progress (x/y), test number, and estimated time remaining."""
    # Determine colors for temp, VR temp, and power based on thresholds
    temp_color = RED if system_info["temp"] >= CONFIG["max_temp_critical"] else ORANGE if system_info["temp"] >= CONFIG["max_temp_warning"] else GREEN
    vrtemp_color = RED if system_info["vrTemp"] >= CONFIG["max_vrtemp_critical"] else ORANGE if system_info["vrTemp"] >= CONFIG["max_vrtemp_warning"] else GREEN
    power_color = RED if system_info["power"] >= CONFIG["max_power_critical"] else ORANGE if system_info["power"] >= CONFIG["max_power_warning"] else GREEN
    
    if monitor_mode:
        # In monitor mode, no time remaining or test progress
        print(f"{GREEN}Status [{datetime.now().strftime('%H:%M:%S')}] Monitor Mode ({reading_count}/∞){RESET}")
    else:
        # Calculate estimated time remaining in hours and minutes
        elapsed_time = time.time() - start_time
        remaining_tests = total_tests - run_number
        time_remaining = (CONFIG["run_duration"] - elapsed_time) + (remaining_tests * CONFIG["run_duration"])
        hours = int(time_remaining // 3600)
        minutes = int((time_remaining % 3600) // 60)
        
        print(f"{GREEN}Status [{datetime.now().strftime('%H:%M:%S')}] Test {run_number}/{total_tests} ({reading_count}/{total_readings}) "
              f"Est. Time Remaining: {hours}h {minutes}m{RESET}")
    
    # Display current, min, max, and avg for specified metrics with color coding for temp, VR temp, and power
    metrics = [
        ("Hashrate", "hashRate", "GH/s", GREEN),  # No color coding for hashrate
        ("J/TH", "jth", "J/TH", GREEN),          # No color coding for J/TH
        ("Temp", "temp", "°C", temp_color),
        ("VR Temp", "vrTemp", "°C", vrtemp_color),
        ("Power", "power", "W", power_color)
    ]
    for label, key, unit, color in metrics:
        avg = sum_values[key] / count_values[key] if count_values[key] > 0 else 0
        print(f"{label}: {color}{system_info[key]:.2f}{RESET} {unit} (Min: {min_values[key]:.2f}, Max: {max_values[key]:.2f}, Avg: {avg:.2f})")
    
    print(f"Frequency: {system_info['frequency']} MHz")
    print(f"Core Voltage: {system_info['coreVoltage']} mV")
    print("-" * 40)

def display_summary(csv_files):
    """Display global min/max summary and best hashrate settings, and log to summaries file."""
    global summaries_filename
    
    # Prepare summary text
    summary_lines = []
    summary_lines.append("=== Global Summary ===")
    summary_lines.append("Min Values:")
    for key, value in global_min_values.items():
        unit = ' MHz' if key == 'frequency' else ' W' if key == 'power' else '°C' if key in ['temp', 'vrTemp'] else ' GH/s' if key == 'hashRate' else ' J/TH' if key == 'jth' else ' mV' if 'Voltage' in key else ' mA'
        summary_lines.append(f"{key.capitalize()}: {value:.2f}{unit}")
    summary_lines.append("\nMax Values:")
    for key, value in global_max_values.items():
        unit = ' MHz' if key == 'frequency' else ' W' if key == 'power' else '°C' if key in ['temp', 'vrTemp'] else ' GH/s' if key == 'hashRate' else ' J/TH' if key == 'jth' else ' mV' if 'Voltage' in key else ' mA'
        summary_lines.append(f"{key.capitalize()}: {value:.2f}{unit}")
    if best_hashrate > 0:
        summary_lines.append(f"\nBest Average Hashrate: {best_hashrate:.2f} GH/s at {best_frequency} MHz, {best_voltage} mV")
    
    # Print to console
    for line in summary_lines:
        print(GREEN + line + RESET)
    
    # Log to summaries file
    try:
        with open(summaries_filename, "a") as f:
            f.write("\n" + "\n".join(summary_lines) + "\n")
    except IOError as e:
        print(RED + f"Error logging global summary to summaries file: {e}" + RESET)
    
    if csv_files:
        print(GREEN + "\nCSV Files:" + RESET)
        print(f"- Readings: {csv_files[0]}")
        if len(csv_files) > 1:
            print(f"- Summaries: {csv_files[1]}")
    else:
        print(ORANGE + "No CSV files generated." + RESET)

def adjust_settings_based_on_values(frequency, core_voltage):
    """Adjust settings based on values.csv entries and current system info."""
    global value_pairs, last_fallback_time, last_fallback_voltage
    if not value_pairs:
        return frequency, core_voltage  # No adjustment if no values loaded

    # Find current position in sorted value_pairs
    current_pair = (core_voltage, frequency)
    try:
        current_index = value_pairs.index(current_pair)
    except ValueError:
        # If current pair not in list, find closest lower voltage
        current_index = 0
        for i, (volt, _) in enumerate(value_pairs):
            if volt >= core_voltage:
                current_index = max(0, i - 1)
                break
        else:
            current_index = len(value_pairs) - 1  # Use highest if voltage exceeds all

    # Check for critical conditions
    critical_hit = (
        system_info["temp"] >= CONFIG["max_temp_critical"] or
        system_info["vrTemp"] >= CONFIG["max_vrtemp_critical"] or
        system_info["power"] >= CONFIG["max_power_critical"]
    )

    if critical_hit:
        # Drop to next lowest voltage/frequency pair
        if current_index > 0:
            new_voltage, new_frequency = value_pairs[current_index - 1]
            reason = ("critical temperature" if system_info["temp"] >= CONFIG["max_temp_critical"] else
                      "critical VR temperature" if system_info["vrTemp"] >= CONFIG["max_vrtemp_critical"] else
                      "critical power")
            print(RED + f"Critical {reason} (Temp: {system_info['temp']:.2f}°C, VR Temp: {system_info['vrTemp']:.2f}°C, "
                        f"Power: {system_info['power']:.2f} W). Dropping to {new_frequency} MHz, {new_voltage} mV." + RESET)
            last_fallback_time = time.time()
            last_fallback_voltage = core_voltage
            return new_frequency, new_voltage
        else:
            # Already at lowest settings
            print(RED + "Critical condition hit but already at lowest settings." + RESET)
            return frequency, core_voltage

    # Check if all critical metrics are at least critical_advance_margin units below critical thresholds
    safe_margin = (
        system_info["temp"] <= CONFIG["max_temp_critical"] - CONFIG["critical_advance_margin"] and
        system_info["vrTemp"] <= CONFIG["max_vrtemp_critical"] - CONFIG["critical_advance_margin"] and
        system_info["power"] <= CONFIG["max_power_critical"] - CONFIG["critical_advance_margin"]
    )

    # Check if enough time has passed since last fallback to allow advancing to a higher voltage
    can_advance = True
    if last_fallback_time is not None and last_fallback_voltage is not None:
        elapsed_time = time.time() - last_fallback_time
        if elapsed_time < CONFIG["advance_delay"]:
            # Prevent advancing to a voltage higher than or equal to the one that caused the fallback
            next_voltage = value_pairs[current_index + 1][0] if current_index < len(value_pairs) - 1 else core_voltage
            if next_voltage >= last_fallback_voltage:
                can_advance = False
                print(ORANGE + f"Advance delayed: {int((CONFIG['advance_delay'] - elapsed_time) / 60)} minutes remaining "
                              f"before advancing to {next_voltage} mV or higher." + RESET)

    if safe_margin and can_advance and current_index < len(value_pairs) - 1:
        # Increase to next highest voltage/frequency pair
        new_voltage, new_frequency = value_pairs[current_index + 1]
        print(GREEN + f"All metrics safe (Temp: {system_info['temp']:.2f}°C, VR Temp: {system_info['vrTemp']:.2f}°C, "
                      f"Power: {system_info['power']:.2f} W). Increasing to {new_frequency} MHz, {new_voltage} mV." + RESET)
        return new_frequency, new_voltage
    else:
        # No adjustment needed
        return frequency, core_voltage

def run_test(frequency, core_voltage, run_number, reboot_threshold, total_tests, monitor_mode=False, values_file=None):
    """Run a single test at specified frequency and core voltage, or monitor indefinitely in monitor mode."""
    global best_hashrate, best_frequency, best_voltage, critical_temp_reached
    if not set_system_settings(frequency, core_voltage):
        print(RED + f"Skipping run {run_number} at {frequency} MHz, {core_voltage} mV" + RESET)
        return None

    print(GREEN + f"Run {run_number}: {frequency} MHz, {core_voltage} mV {'indefinitely' if monitor_mode else f'for {CONFIG['run_duration']}s'}" + RESET)
    start_time = time.time()
    last_log_time = start_time
    reading_count = 0
    total_readings = float('inf') if monitor_mode else int(CONFIG["run_duration"] / CONFIG["status_interval"])  # e.g., 600 / 10 = 60
    run_min_values = {key: float('inf') for key in system_info}
    run_max_values = {key: float('-inf') for key in system_info}
    run_sum_values = {key: 0.0 for key in system_info}
    run_count_values = {key: 0 for key in system_info}
    hashrate_readings = []  # For reboot tracking only
    last_hashrate = None
    identical_hashrate_count = 0
    readings_since_adjustment = 0  # Start at 0 to require initial readings

    while (monitor_mode or time.time() - start_time < CONFIG["run_duration"]) and not is_interrupted:
        # Fetch system info
        if not fetch_system_info(run_min_values, run_max_values, run_sum_values, run_count_values, hashrate_readings):
            print(ORANGE + "Retrying in 10s..." + RESET)
            time.sleep(10)
            continue

        reading_count += 1

        # Check for identical hashrate readings if reboot_threshold is set
        if reboot_threshold is not None:
            current_hashrate = system_info["hashRate"]
            if current_hashrate == last_hashrate:
                identical_hashrate_count += 1
                if identical_hashrate_count >= reboot_threshold:
                    print(ORANGE + f"Detected {identical_hashrate_count} identical hashrate readings ({current_hashrate:.2f} GH/s). Rebooting Bitaxe..." + RESET)
                    log_data(frequency, core_voltage, run_number, note=f"Rebooted due to {identical_hashrate_count} identical hashrate readings")
                    if reboot_bitaxe():
                        time.sleep(30)  # Wait for stabilization
                        identical_hashrate_count = 0  # Reset counter
                        last_hashrate = None
                    else:
                        print(RED + "Reboot failed. Continuing run..." + RESET)
            else:
                identical_hashrate_count = 1
                last_hashrate = current_hashrate

        # In monitor mode with values.csv, adjust settings dynamically if enough readings have been taken
        settings_changed = False
        if monitor_mode and values_file and readings_since_adjustment >= CONFIG["readings_to_advance"]:
            new_frequency, new_core_voltage = adjust_settings_based_on_values(frequency, core_voltage)
            if new_frequency != frequency or new_core_voltage != core_voltage:
                # Apply new settings
                if set_system_settings(new_frequency, new_core_voltage):
                    frequency, core_voltage = new_frequency, new_core_voltage
                    settings_changed = True
                    readings_since_adjustment = 0  # Reset counter
                    note = f"Adjusted to {frequency} MHz, {core_voltage} mV"
                    log_data(frequency, core_voltage, run_number, note=note)
                else:
                    print(RED + f"Failed to adjust settings to {new_frequency} MHz, {new_core_voltage} mV. Continuing with current settings." + RESET)
        elif not values_file:
            # Check for critical conditions only if not using values.csv
            if (system_info["temp"] >= CONFIG["max_temp_critical"] or 
                system_info["vrTemp"] >= CONFIG["max_vrtemp_critical"] or
                system_info["power"] >= CONFIG["max_power_critical"]):
                critical_temp_reached = True
                new_frequency = max(CONFIG["min_frequency"], frequency - 10)
                new_core_voltage = max(CONFIG["min_core_voltage"], core_voltage - 10)
                reason = ("critical temperature" if system_info["temp"] >= CONFIG["max_temp_critical"] else
                          "critical VR temperature" if system_info["vrTemp"] >= CONFIG["max_vrtemp_critical"] else
                          "critical power")
                print(RED + f"Critical {reason} (Temp: {system_info['temp']:.2f}°C, VR Temp: {system_info['vrTemp']:.2f}°C, Power: {system_info['power']:.2f} W). "
                            f"Reducing to {new_frequency} MHz, {new_core_voltage} mV and stopping test." + RESET)
                set_system_settings(new_frequency, new_core_voltage)
                csv_filename = log_data(frequency, core_voltage, run_number,
                                       f"Reduced and stopped due to {reason}")
                if not monitor_mode:
                    # Update best hashrate before stopping
                    if run_count_values["hashRate"] > 0:
                        avg_hashrate = run_sum_values["hashRate"] / run_count_values["hashRate"]
                        if avg_hashrate > best_hashrate:
                            best_hashrate = avg_hashrate
                            best_frequency = frequency
                            best_voltage = core_voltage
                    csv_filename = log_data(frequency, core_voltage, run_number,
                                           min_values=run_min_values, max_values=run_max_values,
                                           sum_values=run_sum_values, count_values=run_count_values)
                # Set best settings or revert to initial
                if not monitor_mode and best_frequency is not None and best_voltage is not None:
                    print(GREEN + f"Setting system to best hashrate settings: {best_frequency} MHz, {best_voltage} mV" + RESET)
                    if not set_system_settings(best_frequency, best_voltage):
                        print(RED + f"Failed to set best hashrate settings. Reverting to initial settings." + RESET)
                        set_system_settings(initial_frequency, initial_core_voltage)
                else:
                    print(ORANGE + "No valid runs completed or in monitor mode. Setting to initial settings." + RESET)
                    set_system_settings(initial_frequency, initial_core_voltage)
                return csv_filename

        # Increment readings counter after adjustment check
        readings_since_adjustment += 1

        # Log data if interval reached
        if time.time() - last_log_time >= CONFIG["log_interval"]:
            csv_filename = log_data(frequency, core_voltage, run_number)
            last_log_time = time.time()

        # Display status only if settings were not changed in this iteration
        if not settings_changed:
            display_status(reading_count, total_readings, run_number, total_tests, start_time, monitor_mode=monitor_mode,
                           min_values=run_min_values, max_values=run_max_values, sum_values=run_sum_values, count_values=run_count_values)

        time.sleep(CONFIG["status_interval"])

    # Update best hashrate at run completion (skipped in monitor mode)
    if not monitor_mode and run_count_values["hashRate"] > 0:
        avg_hashrate = run_sum_values["hashRate"] / run_count_values["hashRate"]
        if avg_hashrate > best_hashrate:
            best_hashrate = avg_hashrate
            best_frequency = frequency
            best_voltage = core_voltage
        csv_filename = log_data(frequency, core_voltage, run_number,
                               min_values=run_min_values, max_values=run_max_values,
                               sum_values=run_sum_values, count_values=run_count_values)
        return csv_filename
    return readings_filename

def main():
    """Main loop for frequency tests or monitor mode."""
    global initial_frequency, initial_core_voltage, bitaxe_ip, best_frequency, best_voltage, critical_temp_reached
    global readings_filename, summaries_filename
    initial_core_voltage, initial_frequency, bitaxe_ip, freq_range, freq_step, reboot_threshold, monitor_mode, values_file = parse_arguments()
    
    # Adjust range and step for monitor mode
    if monitor_mode:
        freq_range = 0
        freq_step = 1  # Avoid division by zero
        total_tests = 1
    else:
        total_tests = ((initial_frequency + freq_range) - (initial_frequency - freq_range)) // freq_step + 1
    
    # Initialize log filenames with voltage first, then frequency
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    readings_filename = f"bitaxe_readings_volt_{initial_core_voltage}_freq_{initial_frequency}_{timestamp}.csv"
    summaries_filename = f"bitaxe_summaries_volt_{initial_core_voltage}_freq_{initial_frequency}_{timestamp}.csv"
    
    # Set initial frequency to initial_frequency - freq_range (or just initial_frequency in monitor mode)
    start_frequency = initial_frequency if monitor_mode else initial_frequency - freq_range
    print(GREEN + f"Requested initial settings: {start_frequency} MHz, {initial_core_voltage} mV" + RESET)
    if not set_system_settings(start_frequency, initial_core_voltage):
        print(RED + "Failed to set initial settings. Exiting." + RESET)
        sys.exit(1)
    
    print(GREEN + f"Initial settings applied: {start_frequency} MHz, {initial_core_voltage} mV, IP: {bitaxe_ip}" + RESET)
    csv_files = [readings_filename]
    if not monitor_mode:
        csv_files.append(summaries_filename)
        print(GREEN + f"Testing from {start_frequency} MHz to {initial_frequency + freq_range} MHz with step {freq_step} MHz" + RESET)
    else:
        print(GREEN + f"Monitoring at {initial_frequency} MHz indefinitely" + RESET)
        if values_file:
            print(GREEN + f"Using values from {values_file} for dynamic adjustments" + RESET)

    if monitor_mode:
        # Single indefinite run in monitor mode
        csv_file = run_test(initial_frequency, initial_core_voltage, 1, reboot_threshold, total_tests, monitor_mode=True, values_file=values_file)
        if csv_file and csv_file not in csv_files:
            csv_files.append(csv_file)
    else:
        # Frequency range testing
        for run_number, freq in enumerate(range(start_frequency, initial_frequency + freq_range + 1, freq_step), 1):
            csv_file = run_test(freq, initial_core_voltage, run_number, reboot_threshold, total_tests)
            if csv_file and csv_file not in csv_files:
                csv_files.append(csv_file)
            if is_interrupted or critical_temp_reached:
                break

    # Set system to best hashrate settings if not already set (skipped in monitor mode)
    if not monitor_mode and not critical_temp_reached:
        if best_frequency is not None and best_voltage is not None:
            print(GREEN + f"Setting system to best hashrate settings: {best_frequency} MHz, {best_voltage} mV" + RESET)
            if not set_system_settings(best_frequency, best_voltage):
                print(RED + f"Failed to set best hashrate settings. Reverting to initial settings." + RESET)
                set_system_settings(initial_frequency, initial_core_voltage)
        else:
            print(ORANGE + "No valid runs completed. Reverting to initial settings." + RESET)
            set_system_settings(initial_frequency, initial_core_voltage)

    if not monitor_mode:
        display_summary(csv_files)
    else:
        print(GREEN + "\nMonitor mode terminated. CSV File:" + RESET)
        print(f"- Readings: {csv_files[0]}")

if __name__ == "__main__":
    main()
