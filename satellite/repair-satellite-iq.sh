#!/usr/bin/env bash
set -Eeuo pipefail

# GroundLink Guardian VM1 SATELLITE: repair only the enp0s8 IQ Netplan definition.
# Run manually from an interactive terminal: sudo bash ./repair-satellite-iq.sh

if (( EUID != 0 )); then
  echo "ERROR: run this script with sudo from an interactive Ubuntu terminal." >&2
  exit 1
fi

command -v netplan >/dev/null
command -v nmcli >/dev/null
command -v ip >/dev/null

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="/var/backups/groundlink-netplan-${timestamp}"
candidate_list="$(mktemp)"
replacement="$(mktemp)"
cleanup() { rm -f -- "$candidate_list" "$replacement"; }
trap cleanup EXIT

echo "== Authoritative Netplan files (before) =="
find /lib/netplan /etc/netplan /run/netplan -maxdepth 1 -type f \
  \( -name '*.yaml' -o -name '*.yml' \) -print 2>/dev/null | sort
echo "== Merged Netplan model (before) =="
netplan get

# Find the persistent /etc/netplan source that explicitly refers to enp0s8.
grep -lR --include='*.yaml' --include='*.yml' -w 'enp0s8' /etc/netplan \
  >"$candidate_list" || true
candidate_count="$(wc -l <"$candidate_list")"
if [[ "$candidate_count" -ne 1 ]]; then
  echo "ERROR: expected exactly one /etc/netplan source mentioning enp0s8; found $candidate_count:" >&2
  sed 's/^/  /' "$candidate_list" >&2
  exit 1
fi
source_file="$(sed -n '1p' "$candidate_list")"

echo "== Selected enp0s8 source =="
echo "$source_file"
sed -n '1,260p' "$source_file"

# Never overwrite a shared definition: this repair must affect only IQ.
if grep -Eq 'enp0s3|enp0s9|10\.0\.2\.|10\.10\.3\.' "$source_file"; then
  echo "ERROR: selected source also mentions NAT or management; refusing to modify it." >&2
  exit 1
fi

echo "== Preservation baseline =="
nat_addr_before="$(ip -o -4 addr show dev enp0s3)"
mgmt_addr_before="$(ip -o -4 addr show dev enp0s9)"
default_before="$(ip -4 route show default)"
forward_before="$(sysctl -n net.ipv4.ip_forward)"
printf '%s\n%s\n%s\nip_forward=%s\n' \
  "$nat_addr_before" "$mgmt_addr_before" "$default_before" "$forward_before"
[[ "$nat_addr_before" == *'10.0.2.15/24'* ]] || { echo "ERROR: unexpected NAT baseline." >&2; exit 1; }
[[ "$mgmt_addr_before" == *'10.10.3.10/24'* ]] || { echo "ERROR: unexpected management baseline." >&2; exit 1; }
[[ "$default_before" == *'dev enp0s3'* ]] || { echo "ERROR: default route is not on enp0s3." >&2; exit 1; }
[[ "$forward_before" == 0 ]] || { echo "ERROR: IPv4 forwarding is not disabled." >&2; exit 1; }

mkdir -m 0700 "$backup_dir"
cp -a /etc/netplan "$backup_dir/netplan"
cp -a "$source_file" "$backup_dir/original-source.yaml"
printf '%s\n' "$source_file" >"$backup_dir/source-path"
ip -br -4 address >"$backup_dir/ip-address.before"
ip -4 route show table main >"$backup_dir/routes.before"
nmcli connection show >"$backup_dir/nm-connections.before"
echo "Backup created: $backup_dir"

# This replacement deliberately contains no gateway, nameserver, or route.
# networkmanager.passthrough is a supported Netplan mapping for NM-only
# properties; it explicitly prevents this isolated link becoming default.
cat >"$replacement" <<'YAML'
network:
  version: 2
  ethernets:
    enp0s8:
      renderer: NetworkManager
      match:
        name: enp0s8
      addresses:
        - 10.10.1.10/24
      dhcp4: false
      networkmanager:
        name: netplan-enp0s8
        uuid: 9a683faa-2cc7-384f-9230-8beaf91f9f29
        passthrough:
          connection.autoconnect: "true"
          ipv4.never-default: "true"
          ipv4.ignore-auto-dns: "true"
YAML

install -o root -g root -m 0600 "$replacement" "$source_file"

echo "== Validate candidate =="
if ! netplan generate; then
  echo "ERROR: validation failed; restoring backup." >&2
  cp -a "$backup_dir/original-source.yaml" "$source_file"
  netplan generate || true
  exit 1
fi
netplan get

echo "== Apply =="
if ! netplan apply; then
  echo "ERROR: apply failed; restoring and re-applying the original source." >&2
  cp -a "$backup_dir/original-source.yaml" "$source_file"
  netplan generate
  netplan apply
  exit 1
fi

echo "== Verify applied configuration =="
nmcli -f DEVICE,TYPE,STATE,CONNECTION device status
nmcli -f connection.id,connection.uuid,connection.interface-name,connection.autoconnect,ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns,ipv4.ignore-auto-dns,ipv4.never-default connection show netplan-enp0s8
ip -br -4 address show dev enp0s8
ip -4 route show dev enp0s8
ip -4 route get 10.10.1.1
ping -n -I enp0s8 -c 2 -W 2 10.10.1.1

echo "== Reconnect persistence test =="
nmcli connection down netplan-enp0s8
nmcli connection up netplan-enp0s8 ifname enp0s8
ip -br -4 address show dev enp0s8 | grep -F '10.10.1.10/24'
ip -4 route show dev enp0s8 | grep -E '^10\.10\.1\.0/24 .*dev enp0s8'

echo "== Preservation and connectivity checks =="
nat_addr_after="$(ip -o -4 addr show dev enp0s3)"
mgmt_addr_after="$(ip -o -4 addr show dev enp0s9)"
default_after="$(ip -4 route show default)"
forward_after="$(sysctl -n net.ipv4.ip_forward)"
[[ "$nat_addr_after" == *'10.0.2.15/24'* ]]
[[ "$mgmt_addr_after" == *'10.10.3.10/24'* ]]
[[ "$default_after" == *'dev enp0s3'* ]]
[[ "$forward_after" == 0 ]]
ping -n -I enp0s3 -c 2 -W 2 1.1.1.1
ping -n -I enp0s9 -c 2 -W 1 10.10.3.20 || true
ping -n -I enp0s9 -c 2 -W 1 10.10.3.30 || true

echo "== SUCCESS =="
echo "Backup: $backup_dir"
echo "Rollback commands:"
echo "  sudo cp -a '$backup_dir/original-source.yaml' '$source_file'"
echo "  sudo netplan generate && sudo netplan apply"
