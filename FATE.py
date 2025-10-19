import os
import subprocess
import re
import hashlib
import json
import configparser
import glob
import time
from datetime import datetime
import sys
import select
import argparse

def parse_nodelist(nodelist_str):
    nodes = set()
    if not nodelist_str: return []
    for part in nodelist_str.strip().split(','):
        if '-' in part:
            start, end = map(int, part.split('-'))
            nodes.update(range(start, end + 1))
        else:
            nodes.add(int(part))
    return sorted(list(nodes))

def get_memory_tier_info():
    tier_info = {}
    base_path = "/sys/devices/virtual/memory_tiering/"
    dir_paths = [p for p in glob.glob(os.path.join(base_path, "memory_tier*")) if os.path.isdir(p)]
    try:
        tier_paths = sorted(dir_paths, key=lambda p: int(os.path.basename(p).replace('memory_tier', '')))
    except (ValueError, IndexError):
        tier_paths = sorted(dir_paths)
    if not tier_paths: return None

    for tier_path in tier_paths:
        tier_name = os.path.basename(tier_path)
        try:
            with open(os.path.join(tier_path, 'nodelist'), 'r') as f: nodelist_str = f.read().strip()
            nodes, total_kb, free_kb = parse_nodelist(nodelist_str), 0, 0
            for n in nodes:
                meminfo_path = f"/sys/devices/system/node/node{n}/meminfo"
                with open(meminfo_path, 'r') as mi:
                    for line in mi:
                        parts = line.split()
                        if 'MemTotal:' in parts: total_kb += int(parts[parts.index('MemTotal:') + 1])
                        elif 'MemFree:' in parts: free_kb += int(parts[parts.index('MemFree:') + 1])
            tier_info[tier_name] = {"nodelist": nodelist_str, "total_kb": total_kb, "free_kb": free_kb}
        except (IOError, FileNotFoundError) as e: print(f"[Error] Could not read info for {tier_name}: {e}")
    return tier_info

def print_memory_tier_info(system_tiers):
    print("=" * 30 + "\n[Info] System Memory Tier Resource Scan\n" + "=" * 30)
    if not system_tiers: print("[Alert] No memory tiers found."); return
    for i, (name, info) in enumerate(system_tiers.items()):
        print(f"Detected Tier {i} -> {name} (nodes: {info['nodelist']})")
        print(f"  - Total: {info['total_kb']:,} kB | Free: {info['free_kb']:,} kB")
    print("-" * 30)

def parse_memory_to_bytes(value_str):
    value_str = value_str.strip().lower()
    units = {"g": 1024**3, "m": 1024**2, "k": 1024}
    if value_str and value_str[-1] in units:
        return int(value_str[:-1]) * units[value_str[-1]]
    return int(value_str)

def apply_memory_tiers(container_id, settings_to_apply):
    print(f"[Info] Applying memory settings for container {container_id[:12]}...")
    cgroup_path = f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope/"
    if not os.path.isdir(cgroup_path): print(f"[Error] Cgroup path not found: {cgroup_path}"); return

    for file_name, byte_value in settings_to_apply.items():
        try:
            full_path = os.path.join(cgroup_path, file_name)
            if not os.path.exists(full_path):
                print(f"[Alert] Cgroup file not found: {file_name}"); continue
            print(f"[Info]     - Writing {int(byte_value)} bytes to {file_name}...")
            with open(full_path, 'w') as f: f.write(str(int(byte_value)))
        except PermissionError: print(f"[Error] Permission denied for {full_path}. Use sudo.")
        except Exception as e: print(f"[Error] Failed to apply setting for {file_name}: {e}")

def get_container_info(container_name):
    result = subprocess.run(['docker', 'inspect', container_name], capture_output=True, text=True, encoding='utf-8')
    if result.returncode != 0: return None
    return json.loads(result.stdout)[0]

def rebalance_dynamic_memory(dynamic_containers, fixed_containers, system_tiers):
    print("=" * 30 + "\n[Info] Rebalancing Memory For Dynamic Containers\n" + "=" * 30)
    if not dynamic_containers: print("[Info] No active dynamic containers to rebalance."); return
    if not system_tiers: print("[Error] No system tier info for rebalancing."); return

    total_requested_limit_bytes = sum(c['limit_bytes'] for c in dynamic_containers)
    
    tier_names = list(system_tiers.keys())
    primary_tier_name = tier_names[0]
    secondary_tier_name = tier_names[1] if len(tier_names) > 1 else None

    tier0_total_bytes = system_tiers[primary_tier_name]["total_kb"] * 1024
    fixed_usage_bytes = sum(c['settings'].get("memory.tiered_memory_0_high", 0) for c in fixed_containers)
    tier0_available_bytes = tier0_total_bytes - fixed_usage_bytes
    print(f"[Info] {primary_tier_name} Status: Total={tier0_total_bytes / 1024**3:.1f}G, Fixed Reserved={fixed_usage_bytes / 1024**3:.1f}G, Available={tier0_available_bytes / 1024**3:.1f}G")

    if total_requested_limit_bytes <= tier0_available_bytes:
        print("[Info] Total dynamic request fits in primary tier. Assigning directly.")
        for c in dynamic_containers:
            settings = {"memory.tiered_memory_0_high": c['limit_bytes'], "memory.tiered_memory_1_high": 0}
            apply_memory_tiers(c['id'], settings)
    else:
        print("[Alert] Total dynamic request exceeds primary tier. Allocating proportionally.")
        tier1_available = system_tiers.get(secondary_tier_name, {}).get("total_kb", 0) * 1024
        for c in dynamic_containers:
            proportion = c['limit_bytes'] / total_requested_limit_bytes
            tier0_alloc = proportion * tier0_available_bytes
            tier1_alloc = c['limit_bytes'] - tier0_alloc
            if tier1_alloc > 0 and (not secondary_tier_name or tier1_available == 0):
                print(f"[Alert] Container {c['name']} requires secondary tier, but none is available.")
            settings = {"memory.tiered_memory_0_high": tier0_alloc, "memory.tiered_memory_1_high": tier1_alloc}
            apply_memory_tiers(c['id'], settings)

def cleanup_old_backup_images(container_name):
    print(f"[Info] Cleaning up old backup images for '{container_name}'...")
    pattern = f"{container_name.lower()}_*"
    
    format_str = "{{.Repository}}\t{{.CreatedAt}}"
    find_cmd = ['docker', 'images', '--filter', f'reference={pattern}', '--format', format_str]
    result = subprocess.run(find_cmd, capture_output=True, text=True, encoding='utf-8')
    
    if result.returncode != 0 or not result.stdout.strip():
        print("[Info] No backup images found to clean up.")
        return

    images = []
    for line in result.stdout.strip().split('\n'):
        repo, created_at = line.split('\t')
        images.append((created_at, repo))
    
    images.sort()

    images_to_delete = [repo for _, repo in images[:-1]]
    
    if not images_to_delete:
        print("[Info] Only one backup image exists. No cleanup needed.")
        return

    print(f"[Info] Found {len(images_to_delete)} old backup image(s) to delete.")
    
    rmi_cmd = ['docker', 'rmi'] + images_to_delete
    delete_result = subprocess.run(rmi_cmd, capture_output=True, text=True, encoding='utf-8')

    if delete_result.returncode == 0:
        print("[Info] Successfully deleted old backup images.")
    else:
        print(f"[Warning] Could not delete old backup images. Error: {delete_result.stderr.strip()}")


def run_docker_containers(directory='./Containers.d'):
    if not os.path.isdir(directory): print(f"[Error] Directory '{directory}' not found."); return
    
    system_memory_tiers = get_memory_tier_info()
    print_memory_tier_info(system_memory_tiers)

    recognized_configs, containers_started = 0, 0
    name_pattern = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]+$')
    all_filepaths = [os.path.join(directory, f) for f in os.listdir(directory) if os.path.isfile(os.path.join(directory, f))]
    
    parsed_configs, active_fixed, active_dynamic = [], [], []

    print("="*30 + "\n[Info] Synchronizing Container States & Applying Fixed Memory\n" + "="*30)
    for filepath in all_filepaths:
        filename = os.path.basename(filepath)
        print(f"[Info] Processing file '{filename}'...")
        try:
            config = configparser.ConfigParser()
            with open(filepath, 'r', encoding='utf-8') as f: all_lines = f.readlines()
            
            config_lines, standalone_options = [], []
            for line in all_lines:
                stripped = line.strip()
                if stripped.startswith('[') or '=' in stripped or not stripped: config_lines.append(line)
                elif stripped: standalone_options.append(stripped)
            
            config.read_string("".join(config_lines))
            current_config_hash = hashlib.sha256("".join(all_lines).encode('utf-8')).hexdigest()

            image = config.get('General', 'image', fallback='').strip("'\"")
            container_name = config.get('General', 'name', fallback='').strip("'\"")

            if not image or not container_name: print(f"[Error] Skipping '{filename}': 'image'/'name' required."); continue
            if not name_pattern.match(container_name): print(f"[Error] Skipping '{filename}': Invalid name '{container_name}'"); continue
            
            recognized_configs += 1
            existing_info = get_container_info(container_name)
            
            def handle_running_container(name, current_config):
                info = get_container_info(name)
                if not (info and info['State']['Running']): time.sleep(1); info = get_container_info(name)
                if not (info and info['State']['Running']): print(f"[Error] Container '{name}' failed to enter running state."); return

                if system_memory_tiers and current_config.has_section('Memory_Control'):
                    mem_type = current_config.get('Memory_Control', 'type')
                    if mem_type == 'fixed':
                        settings = {}
                        for i in range(len(system_memory_tiers)):
                            config_key = f'memory_tier{i}'
                            if current_config.has_option('Memory_Control', config_key):
                                limit_str = current_config.get('Memory_Control', config_key)
                                settings[f"memory.tiered_memory_{i}_high"] = parse_memory_to_bytes(limit_str)
                        if settings:
                            apply_memory_tiers(info['Id'], settings)
                            active_fixed.append({'id': info['Id'], 'name': name, 'settings': settings})
                    elif mem_type == 'dynamic':
                        limit_str = current_config.get('Memory_Control', 'memory_limit')
                        active_dynamic.append({'id': info['Id'], 'name': name, 'limit_bytes': parse_memory_to_bytes(limit_str)})
            
            if not existing_info:
                command = ['docker', 'run', '--label', f'config.hash={current_config_hash}', '--name', container_name] + [item for opt in standalone_options for item in opt.split()] + [image]
                result = subprocess.run(command, capture_output=True, text=True)
                if result.returncode == 0:
                    containers_started += 1; print(f"[Info] Success: New container '{container_name}' started.")
                    handle_running_container(container_name, config)
                else: print(f"[Error] Failure creating '{container_name}'.\n[Error]    - {result.stderr.strip()}")
            else:
                if existing_info['Config']['Labels'].get('config.hash') == current_config_hash:
                    if existing_info['State']['Running']:
                        print(f"[Info] Container '{container_name}' is up-to-date and running.")
                        handle_running_container(container_name, config)
                    else:
                        result = subprocess.run(['docker', 'start', container_name], capture_output=True, text=True)
                        if result.returncode == 0:
                            containers_started += 1; print(f"[Info] Success: Container '{container_name}' started.")
                            handle_running_container(container_name, config)
                        else: print(f"[Error] Failure starting '{container_name}'.\n[Error]    - {result.stderr.strip()}")
                else:
                    print(f"[Info] Config for '{container_name}' changed. Recreating.")
                    timestamp = datetime.now().strftime('%Y%m%d%H%M%S'); backup_image_name = f"{container_name.lower()}_{timestamp}"
                    try:
                        if existing_info['State']['Running']:
                            subprocess.run(['docker', 'stop', container_name], check=True, capture_output=True)
                        subprocess.run(['docker', 'commit', '-p', container_name, backup_image_name], check=True, capture_output=True)
                        print(f"[Info] Created new backup image: {backup_image_name}")
                        subprocess.run(['docker', 'rm', container_name], check=True, capture_output=True)
                        print(f"[Info] Removed old container '{container_name}'.")
                        cleanup_old_backup_images(container_name)
                        command = ['docker', 'run', '--label', f'config.hash={current_config_hash}', '--name', container_name] + [item for opt in standalone_options for item in opt.split()] + [backup_image_name]
                        result = subprocess.run(command, capture_output=True, text=True)
                        if result.returncode == 0:
                            containers_started += 1; print(f"[Info] Success: Recreated container '{container_name}'.")
                            handle_running_container(container_name, config)
                        else: raise Exception(f"Failed to create new container: {result.stderr.strip()}")
                    except Exception as e: print(f"[Error] An error occurred during recreation: {e}")
            print("-" * 30)
        except Exception as e: print(f"[Error] Critical error processing '{filename}': {e}")
        
    if system_memory_tiers:
        rebalance_dynamic_memory(active_dynamic, active_fixed, system_memory_tiers)

    print("=" * 30 + f"\n[Info] Summary: Recognized {recognized_configs} configurations, managed {containers_started} containers.\n" + "=" * 30)

def get_container_names_from_configs(directory='./Containers.d'):
    if not os.path.isdir(directory):
        print(f"[Error] Directory '{directory}' not found.")
        return []
        
    names = []
    all_filepaths = [os.path.join(directory, f) for f in os.listdir(directory) if os.path.isfile(os.path.join(directory, f))]
    for filepath in all_filepaths:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                all_lines = f.readlines()

            config_lines = []
            for line in all_lines:
                stripped = line.strip()
                if stripped.startswith('[') or '=' in stripped or not stripped:
                    config_lines.append(line)

            config = configparser.ConfigParser()
            config.read_string("".join(config_lines)) # Read only the filtered lines

            if config.has_option('General', 'name'):
                names.append(config.get('General', 'name').strip("'\""))
        except configparser.Error as e:
            print(f"[Alert] Could not properly parse '{os.path.basename(filepath)}': {e}")

    return names

def monitor_containers(to_screen, to_file):
    print("=" * 30 + "\n[Info] Starting Memory Tier Monitoring Mode\n" + "=" * 30)
    
    container_names = get_container_names_from_configs()
    if not container_names:
        print("[Alert] No containers found in configuration files to monitor.")
        return

    log_file_handler = None
    if to_file:
        log_filename = f"memory_monitor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        log_file_handler = open(log_filename, 'w', encoding='utf-8')
        print(f"[Info] Logging to file: {log_filename}")

    print("Press 'q' then 'Enter' to quit.\n")
    
    container_id_cache = {}

    try:
        while True:
            if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
                line = sys.stdin.readline()
                if 'q' in line:
                    break
            
            output_lines = []
            output_lines.append(f"--- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")

            for name in container_names:
                container_id = container_id_cache.get(name)
                if not container_id:
                    info = get_container_info(name)
                    if info and info['State']['Running']:
                        container_id = info['Id']
                        container_id_cache[name] = container_id
                    else:
                        output_lines.append(f"{name}: Not running or not found.")
                        continue
                
                cgroup_path = f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope/"
                numa_stat_file = os.path.join(cgroup_path, "memory.numa_stat")
                
                try:
                    with open(numa_stat_file, 'r') as f:
                        numa_stat_content = f.read()
                    
                    # node 0의 anon + file 계산
                    node0_cmd = f"""awk '
                        $1=="anon" {{ split($2,a,"="); split($3,b,"="); anon0=a[2]; anon1=b[2] }}
                        $1=="file" {{ split($2,a,"="); split($3,b,"="); file0=a[2]; file1=b[2] }}
                        END {{ print anon0+file0 }}
                    ' {numa_stat_file}"""
                    node0 = subprocess.check_output(node0_cmd, shell=True, text=True).strip()
                    
                    # node 1의 anon + file 계산
                    node1_cmd = f"""awk '
                        $1=="anon" {{ split($2,a,"="); split($3,b,"="); anon0=a[2]; anon1=b[2] }}
                        $1=="file" {{ split($2,a,"="); split($3,b,"="); file0=a[2]; file1=b[2] }}
                        END {{ print anon1+file1 }}
                    ' {numa_stat_file}"""
                    node1 = subprocess.check_output(node1_cmd, shell=True, text=True).strip()
                    
                    output_lines.append(f"{name}.memory.numa_stat.node0: {int(node0):,}")
                    output_lines.append(f"{name}.memory.numa_stat.node1: {int(node1):,}")
                
                except FileNotFoundError:
                    output_lines.append(f"{name}.memory.numa_stat: File not found.")
                except (IOError, PermissionError) as e:
                    output_lines.append(f"{name}.memory.numa_stat: Error reading file - {e}")
                except subprocess.CalledProcessError as e:
                    output_lines.append(f"{name}.memory.numa_stat: Error processing awk - {e}")
            
            output_lines.append("-" * 30)
            output_string = "\n".join(output_lines)
            
            if to_screen:
                os.system('cls' if os.name == 'nt' else 'clear')
                print(output_string)
                print("\nPress 'q' then 'Enter' to quit.")

            if to_file and log_file_handler:
                log_file_handler.write(output_string + "\n\n")
                log_file_handler.flush()
            
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n[Info] Monitoring stopped by user.")
    finally:
        if log_file_handler:
            log_file_handler.close()
            print("[Info] Log file closed.")
        print("[Info] Exiting monitoring mode.")

def terminate_containers():
    print("=" * 30 + "\n[Info] Terminating All Managed Containers\n" + "=" * 30)
    
    container_names = get_container_names_from_configs()
    if not container_names:
        print("[Alert] No containers found in configuration files to terminate.")
        return

    for name in container_names:
        print(f"[Info] Processing container '{name}'...")
        
        stop_result = subprocess.run(['docker', 'stop', name], capture_output=True, text=True)
        if stop_result.returncode == 0:
            print(f"  - Success: Stopped container '{name}'.")
        else:
            if "No such container" in stop_result.stderr:
                print(f"  - Info: Container '{name}' was not running.")
            else:
                print(f"  - Warning: Could not stop container '{name}'.\n    Error: {stop_result.stderr.strip()}")
        
        print("-" * 30)
    
    print("[Info] Termination process complete.")


if __name__ == '__main__':
    
    parser = argparse.ArgumentParser(
        description="A tool to manage and monitor Docker containers with specific controls for tiered memory systems.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""Usage examples:
  sudo python3 FATE.py               # Start/update containers based on config files.
  sudo python3 FATE.py -m            # Monitor memory usage on the screen.
  sudo python3 FATE.py -m -f         # Monitor on screen AND log to a file.
  sudo python3 FATE.py -t            # Stop and remove all managed containers.
  sudo python3 FATE.py --help        # Show this help message."""
    )
    parser.add_argument('-m', '--monitor', action='store_true', help='Monitor container memory usage on screen.')
    parser.add_argument('-f', '--file', action='store_true', help='Log container memory usage to a file.')
    parser.add_argument('-t', '--terminate', action='store_true', help='Stop and remove all managed containers.')
    args = parser.parse_args()

    if args.monitor or args.file:
        monitor_containers(to_screen=args.monitor, to_file=args.file)
    elif args.terminate:
        terminate_containers()
    else:
        run_docker_containers()
