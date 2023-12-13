---
description: Generates and displays the user's productivity in the latest recording.
---

# productivity.py

Usage:

```
$ python -m openadapt.productivity
```

##

## 1 Introduction

The purpose of this document is to define the requirements and design of the Productivity measurement system in the OpenAdapt process automation library.

## 2 Background

* Motivation:
  * Useful for businesses to evaluate the performance of their employees through their productivity
  * Convenient for us to measure this productivity since we are recording the user’s actions&#x20;
  * Businesses would be more inclined to use OpenAdapt if they knew how it helped with their productivity&#x20;

## 3 Goals

* We want to be able to provide businesses with metrics that show how productive a user is in a recording. These metrics include but are not limited to:
  * Number and length of repetitive tasks
  * Total/average time spent on repetitive tasks
  * Number of mouse clicks
  * Number of key presses
  * Number of long pauses
  * Number of window/tab changes
  * Total time spent on each window/tab
  * Number of errors (at the moment we don’t have a good way to do this)

## 4 Design

### 4.1 System Overview

![](https://lh4.googleusercontent.com/3TyRRd1K\_dYiOlyBFWsMBIdzABcY2o\_IldelWRsisBuxF\_WyNCBpCsDuG6SpMzA5pcC-RnJMEZ\_CY2tzX99K94g-fVrRrozn-02keOqXPK3-QBJT3Sszi8GBIpNFIViKwoneGjsTVAr\_g\_V4dNqh790)[Original file](https://drive.google.com/file/d/1mw24lx-uWfxmHtK9K8iyT8iEaWCBU5Kv/view?usp=drive\_link)

### 4.2 Task identification

* Recursively use the [longest repeated non-overlapping substring algorithm](https://www.geeksforgeeks.org/longest-repeating-and-non-overlapping-substring/) to find repeating lists of ActionEvents until the final list contains a single task and no repetition&#x20;
* O(n^2), room for optimization

### 4.3 Window event data

* Show one screenshot for each time the user switches tabs/windows, accompanied by data like # of clicks, # keypresses, and time spent on the tab&#x20;

### 4.4 Visualization

* Everything described above is visualized as an HTML page using bokeh, similarly to the original visualize.py
* At the top of the HTML page there is data about the entire recording, including things like # of tasks completed, total/average time spent on repetitive tasks, # keypresses/clicks, # pauses, # tab changes

## 5 Analysis

### 5.1 Performance

Plots and discussion of relevant metrics (e.g. accuracy, throughput)

## 6 Future Work

* Refactor to make a clean, sleek webpage rather than just visualize HTML
* Ultimately, it would be nice to be able to compare the productivity of a user with that of a ReplayStrategy - time spent on completing the tasks in a recording and error rate would be a useful comparison
* Once we have a vision model that is suitable for GUI images, we can use that model to identify the task identified as the longest repeating task in the recording
* Ideally, we would get feedback from businesses about what information suits their needs
