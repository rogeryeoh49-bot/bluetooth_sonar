# Bluetooth Sonar

> A fun, visually polished Bluetooth signal tracker with a sonar-inspired interface, enhanced signal-processing algorithms, and real-time directional guidance.

Bluetooth Sonar is a small experimental project that turns Bluetooth signal data into an interactive sonar-style tracking experience.

The idea is simple: instead of looking at a list of Bluetooth devices, why not make finding one feel like using a detector?

The project combines Bluetooth scanning, signal-strength analysis, filtering algorithms, distance estimation, and audio feedback into a visual and interactive tracking tool.

It is primarily a fun project and an experiment in making Bluetooth tracking more intuitive, visual, and enjoyable.

---

## ✨ Features

### 📡 Sonar-Inspired Interface

The interface is designed to resemble a classic sonar / signal detection system.

Nearby Bluetooth devices appear as targets around the sonar display, creating a visual representation of the surrounding Bluetooth environment.

Different devices are assigned approximate positions and distance indicators based on their observed signal strength.

---

### 🎯 Precise Device Tracking

Select a Bluetooth device and switch from general scanning to focused tracking.

The tracker continuously analyzes the selected device's Bluetooth signal and provides directional feedback intended to help you physically locate it.

You can specifically track a selected Bluetooth device address rather than repeatedly scanning the entire environment.

---

### 🧠 Enhanced Signal Processing

Bluetooth signal strength can be noisy and unstable.

Instead of simply displaying raw RSSI values, the project applies additional processing and filtering techniques to make the tracking experience more stable and useful.

The system attempts to reduce sudden fluctuations and extract a more meaningful signal trend from noisy Bluetooth measurements.

---

### 📋 Fixed Device List

The application maintains a fixed list of discovered devices instead of constantly rebuilding the entire interface.

This helps keep the UI stable while signal measurements are updated in real time.

Devices can therefore remain visually consistent while their signal information changes.

---

### ⚡ Reduced Unnecessary Refreshing

The project includes logic designed to avoid unnecessary UI and data refreshes.

Rather than repeatedly recreating the entire device list whenever a Bluetooth measurement changes, updates are focused on the information that actually needs to change.

This makes the interface feel significantly smoother during continuous scanning and tracking.

---

### 📶 Real-Time Signal Updates

Signal measurements are continuously updated while tracking.

The interface can reflect changes in Bluetooth signal strength as you move around the environment.

This makes it possible to use the application as an interactive proximity detector rather than simply a Bluetooth device scanner.

---

### 📏 Approximate Distance Indication

The sonar interface provides an approximate distance indication for detected devices.

Distance is estimated from Bluetooth signal characteristics and should be treated as a relative indication rather than a precise measurement.

The purpose is to communicate whether a device is generally getting closer or farther away.

---

### 🔊 Audio Proximity Feedback

The project also includes an audible proximity indicator inspired by physical signal detectors.

As the tracked device gets closer, the beeping becomes faster.

As the device gets farther away, the beeping slows down.

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
