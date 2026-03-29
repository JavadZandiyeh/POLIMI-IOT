import csv
import json
import re
import os

from matplotlib import pyplot as plt
import numpy as np


PERSON_CODE = "11044962"
US_TO_S = 1e-6


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def read_csv(filename):
    data = []
    with open(filename) as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            data.append(float(row[1]))
    return data

def read_txt(filename):
    with open(filename, encoding="utf-8") as f:
        return f.read()

def avg(vals):
    return sum(vals) / len(vals)

def store_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def battery_energy_joules_from_person_code(person_code):
    s = str(person_code).strip()
    abcd = int(s[-4:])
    return (abcd % 5000) + 15000


# ---------------------------------------------------------------------------
# Initialize data
# ---------------------------------------------------------------------------

deep_sleep_data = read_csv("files/deep_sleep.csv")
sender_data = read_csv("files/sender.csv")
sensor_data = read_csv("files/sensor-read.csv")
avg_power_states = dict()

# ---------------------------------------------------------------------------
# Calculate average power per state from CSV files
# ---------------------------------------------------------------------------

# sender.csv: Baseline vs TX Spikes
tx_baseline = [d for d in sender_data if d < 625]
tx_spikes_13dBm = [d for d in sender_data if 665 > d >= 625] # For the 13 dBm TX power, the spike is at 665 mW

avg_power_states["sender.csv"] = {
    "tx_baseline_mW": avg(tx_baseline),
    "tx_spike_13dBm_mW": avg(tx_spikes_13dBm),
}

# deep_sleep.csv: Deep Sleep (<80), Boot (280-550), Idle WiFi Off (200-280), WiFi On (>=550)
ds_sleep = [d for d in deep_sleep_data if d < 80]
ds_boot = [d for d in deep_sleep_data if 280 <= d < 550]
ds_idle = [d for d in deep_sleep_data if 200 <= d < 280]
ds_wifi_on = [d for d in deep_sleep_data if d >= 550]

avg_power_states["deep_sleep.csv"] = {
    "ds_sleep_mW": avg(ds_sleep),
    "ds_boot_mW": avg(ds_boot),
    "ds_wifi_off_mW": avg(ds_idle),
    "ds_wifi_on_mW": avg(ds_wifi_on),
}

# sensor-read.csv: Idle (<280mW) and Sensor Reading (>=280mW)
sr_idle = [d for d in sensor_data if d < 280]
sr_read = [d for d in sensor_data if d >= 280]

avg_power_states["sensor-read.csv"] = {
    "sr_idle_mW": avg(sr_idle),
    "sr_read_mW": avg(sr_read),
}

os.makedirs("outputs", exist_ok=True)

store_json("outputs/avg_power_states.json", avg_power_states)

# ---------------------------------------------------------------------------
# Energy consumption estimation using Wokwi data and average power per state
# ---------------------------------------------------------------------------

text = read_txt("example-run-output.txt")

num_cycles = len(re.findall(r"deep_sleep_s: ([\d.]+)", text))

# (label, log key pattern, csv file, power state, scale log value -> seconds)
phases = [
    ("boot",         r"boot_us: (\d+)",         "deep_sleep.csv",  "ds_boot_mW",        US_TO_S),
    ("wifi_on",      r"wifi_on_us: (\d+)",      "deep_sleep.csv",  "ds_wifi_on_mW",     US_TO_S),
    ("wifi_off",     r"wifi_off_us: (\d+)",     "deep_sleep.csv",  "ds_wifi_off_mW",    US_TO_S),
    ("sensor_idle",  r"sensor_idle_us: (\d+)",  "sensor-read.csv", "sr_idle_mW",        US_TO_S),
    ("sensor_read",  r"sensor_read_us: (\d+)",  "sensor-read.csv", "sr_read_mW",        US_TO_S),
    ("sender_spike", r"sender_spike_us: (\d+)", "sender.csv",      "tx_spike_13dBm_mW", US_TO_S),
    ("sender_idle",  r"sender_idle_us: (\d+)",  "sender.csv",      "tx_baseline_mW",    US_TO_S),
    ("deep_sleep",   r"deep_sleep_s: ([\d.]+)", "deep_sleep.csv",  "ds_sleep_mW",       1.0),
]

# Skip first cycle (POWERON_RESET); compute energy = avg_power * avg_time
avg_running_times_s = {}
energy_consumption_mj = {}

for label, pattern, csv_file, state, to_seconds in phases:
    vals = [float(x) for x in re.findall(pattern, text)]
    t = avg(vals[1:]) * to_seconds  # seconds
    P = avg_power_states[csv_file][state]  # milliwatts
    E = P * t  # millijoules
    avg_running_times_s[f"{label}_s"] = t
    energy_consumption_mj[f"{label}_mJ"] = E

total_per_cycle_mj = sum(energy_consumption_mj.values())
energy_consumption_mj["total_per_cycle_mJ"] = total_per_cycle_mj

store_json("outputs/avg_running_times.json", avg_running_times_s)
store_json("outputs/avg_energy_consumption.json", energy_consumption_mj)

# ---------------------------------------------------------------------------
# Create power vs time graph of multiple cycles
# ---------------------------------------------------------------------------

for label, pattern, csv_file, state, to_seconds in phases:
    if csv_file == "sender.csv" and label == "sender_spike":
        sender_spike = [float(x) for x in re.findall(pattern, text)]
    if csv_file == "sender.csv" and label == "sender_idle":
        sender_idle = [float(x) for x in re.findall(pattern, text)]
    if csv_file == "deep_sleep.csv" and label == "boot":
        deep_sleep_boot = [float(x) for x in re.findall(pattern, text)]
    if csv_file == "deep_sleep.csv" and label == "wifi_on":
        deep_sleep_wifi_on = [float(x) for x in re.findall(pattern, text)]
    if csv_file == "deep_sleep.csv" and label == "wifi_off":
        deep_sleep_wifi_off = [float(x) for x in re.findall(pattern, text)]
    if csv_file == "deep_sleep.csv" and label == "deep_sleep":
        deep_sleep_sleep = [float(x) for x in re.findall(pattern, text)]
    if csv_file == "sensor-read.csv" and label == "sensor_idle":
        sensor_idle = [float(x) for x in re.findall(pattern, text)]
    if csv_file == "sensor-read.csv" and label == "sensor_read":
        sensor_read = [float(x) for x in re.findall(pattern, text)]

# sender.csv
sender_times = []
for i in range(1, num_cycles):
    sender_times.append(sender_spike[i])
    sender_times.append(sender_idle[i])

sender = avg_power_states["sender.csv"]
p_tx_spike_13dBm, p_tx_baseline = sender["tx_spike_13dBm_mW"], sender["tx_baseline_mW"]
sender_powers = [p_tx_spike_13dBm, p_tx_baseline] * (num_cycles - 1)

# deep_sleep.csv
deep_sleep_times = []
for i in range(1, num_cycles):
    deep_sleep_times.append(deep_sleep_boot[i])
    deep_sleep_times.append(deep_sleep_wifi_off[i] / 2)
    deep_sleep_times.append(deep_sleep_wifi_on[i])
    deep_sleep_times.append(deep_sleep_wifi_off[i] / 2)
    deep_sleep_times.append(deep_sleep_sleep[i] * 10000)

ds = avg_power_states["deep_sleep.csv"]
p_ds_sleep, p_ds_boot, p_ds_wifi_off, p_ds_wifi_on = ds["ds_sleep_mW"], ds["ds_boot_mW"], ds["ds_wifi_off_mW"], ds["ds_wifi_on_mW"]
deep_sleep_powers = [p_ds_boot, p_ds_wifi_off, p_ds_wifi_on, p_ds_wifi_off, p_ds_sleep] * (num_cycles - 1)

# sensor-read.csv
sensor_read_times = []
for i in range(1, num_cycles):
    sensor_read_times.append(sensor_read[i])
    sensor_read_times.append(sensor_idle[i])

sensor = avg_power_states["sensor-read.csv"]
p_sensor_read, p_sensor_idle = sensor["sr_read_mW"], sensor["sr_idle_mW"]
sensor_powers = [p_sensor_read, p_sensor_idle] * (num_cycles - 1)


# plots
os.makedirs("plots", exist_ok=True)

def plot_step_power_vs_time(times, powers, title, path):
    times = np.array(times, dtype=float)
    powers = np.array(powers, dtype=float)
    time_edges = np.concatenate(([0], np.cumsum(times)))
    plt.figure()
    plt.step(time_edges[:-1], powers, where="post")
    plt.xlabel("Time (µs)")
    plt.ylabel("Power (mW)")
    plt.title(title)
    plt.grid()
    plt.tight_layout()
    plt.savefig(path)
    plt.close()


plot_step_power_vs_time(
    sender_times,
    sender_powers,
    "Improved Power Consumption (Sender)",
    "plots/power_vs_time_sender.png",
)

plot_step_power_vs_time(
    deep_sleep_times,
    deep_sleep_powers,
    "Improved Power Consumption (Deep-Sleep Cycle)",
    "plots/power_vs_time_deep_sleep.png",
)

plot_step_power_vs_time(
    sensor_read_times,
    sensor_powers,
    "Improved Power Consumption (Idle - Sensor Read)",
    "plots/power_vs_time_sensor_read.png",
)

# ---------------------------------------------------------------------------
# Calculate the operating lifetime
# ---------------------------------------------------------------------------

battery_energy_mJ = battery_energy_joules_from_person_code(PERSON_CODE) * 1000

lifetime_s = battery_energy_mJ / total_per_cycle_mj
hours, rem = divmod(lifetime_s, 3600)
minutes, seconds = divmod(rem, 60)

lifetime_str = f"Operating lifetime: {int(hours)} h {int(minutes)} min {seconds:.2f} s"
print(lifetime_str)

os.makedirs("outputs", exist_ok=True)
with open("outputs/operating_lifetime.txt", "w") as f:
    f.write(lifetime_str + "\n")