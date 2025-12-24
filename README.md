# FATE: Fairness-Aware memory allocation for containers in Tiered memory Environments

**FATE** is a system designed to achieve fair and efficient allocation of containers in a NUMA (Non-Uniform Memory Access) tiered-memory environment. It modifies the Linux kernel's `memcg` (memory control group) to track memory usage on a per-tier basis and utilizes this information to schedule containers to their optimal locations.


## 🎯 Core Goals

* **Maximize Efficiency in Tiered Memory:** To improve overall system performance by allocating containers to the most suitable memory tier (e.g., DRAM, PMem, CXL) based on their characteristics.
* **Ensure Fairness Among Containers:** To provide a stable execution environment for all containers by implementing fair allocation policies that prevent any single container from monopolizing high-performance memory.


## ✨ Key Features

### 1. Per-Tier Memory Usage Tracking in `memcg`

We have extended the Linux kernel's `memcg` functionality to monitor container memory usage for each NUMA node and memory tier.

* **Tier-specific `memory.current`:** Allows real-time tracking of how much memory a container is using on each specific memory tier.
* **Tier-specific `memory.high`:** Implement a per-tier memory.high in memcg to enforce a soft limit on memory usage for specific tiers.

This feature is crucial for analyzing the memory access patterns of containers and serves as the core data source for our fair allocation system.

### 2. Fairness-aware Container Placement System (In Development)

We are currently developing a system that leverages the per-tier memory usage data to place containers on the most appropriate NUMA node and memory tier.

* **Real-time Monitoring:** Continuously monitors the memory usage of each container across different tiers.
* **Intelligent Placement Policies:** Will dynamically schedule or migrate containers based on policies that consider both fairness and performance.


## 🚧 Project Status

* **`memcg` Modification & Per-Tier Tracking:** **Done**
* **Fairness-aware Container Placement System:** **In Progress**
* **Project Based on Linux 6.15.6**
