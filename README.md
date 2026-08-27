# Bluetooth Sonar

> A fun little Bluetooth tracker with a sonar-inspired interface.

This is a small project I made to experiment with Bluetooth signal tracking and visualization.

Instead of showing Bluetooth devices as a boring list, it turns them into targets on a sonar-style interface. Different devices show an approximate distance, and the display updates in real time as the signal changes.

## Features

- Sonar-inspired interface
- Real-time Bluetooth signal tracking
- Approximate distance indication
- Select and track a specific Bluetooth device
- Signal filtering and improved tracking algorithms
- Fixed device list to avoid unnecessary UI refreshing
- Audio feedback — the closer you get, the faster it beeps

## A Little Experiment

I tested it with an AirPods charging case, and honestly, it worked better than I expected.

In my testing, it felt faster and more directional than Find My when I was already somewhere near the case. The combination of signal strength, visual feedback, and the increasingly faster beeps made it surprisingly easy to follow.

Of course, Bluetooth signal strength isn't perfect, so the distance and direction are only approximate.

## Tech

- Python
- Bluetooth / BLE
- RSSI
- Signal processing
- Real-time visualization

## Getting Started

```bash
git clone https://github.com/rogeryeoh49-bot/your-repository-name.git
cd your-repository-name
pip install -r requirements.txt
python main.py
This means you do not always need to look at the screen while searching.

```text
Farther away
    ↓
slow   ·     ·     ·     ·

Closer
    ↓
faster   ·  ·  ·  ·  ·  ·

Very close
    ↓
very fast   · · · · · · · ·
