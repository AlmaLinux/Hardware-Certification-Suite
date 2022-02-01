#!/usr/bin/env bash

DMIDECODE="/usr/sbin/dmidecode"
RED='\033[0;31m'
NC="\033[0m"
AQUA="\033[0;96m"

function dmi_info(){
  local type=$1
  local info_string=$2
  ifs_storage="${IFS}"
  IFS=$'\n'
  highlight_pattern="((Version)|(Serial Number)|(Vendor)|(Product Name)|(Manufacturer)|(Family:)|(UUID)|(ID))"
  info="$(${DMIDECODE} -t ${type} | grep -v "^Handle 0x" | grep -v "^# dmidecode" | grep -v "^Getting SMBIOS data" | grep -v "^SMBIOS")"
  echo ""
  printf "${RED}%s${NC}\n" "${info_string}"
  for line in ${info}; do
    if [ $(echo $line | grep -c -E "${highlight_pattern}") -eq 0 ]; then
        echo $line
    else
        echo $line | GREP_COLORS="ms=01;32" grep -E --color=always "${highlight_pattern}"
    fi
  done
  IFS="${ifs_storage}"
}

function bus_devices(){
  info="$(lspci -vmm)"
  echo "${info}"
  echo ""
}

function network_cards(){
  info="$(lshw -class network)"
  echo "${info}"
  echo ""
}

function scsi_devices(){
  info="$(lsscsi)"
  echo "${info}"
  echo ""
}

function block_devices(){
  info="$(lsblk -d -o name,rota,size,type,mountpoint,model,serial)"
  echo "${info}"
  echo ""
}

printf "${AQUA}System Hardware Components report${NC}\n"
dmi_info "bios" "BIOS Report"
dmi_info "system" "System Report"
dmi_info "baseboard" "Base Board Report"
dmi_info "cache" "Cache Report"
dmi_info "processor" "Processor Report"
dmi_info "memory" "Memory Report"

printf "${AQUA}PCI Devices${NC}\n"
bus_devices

printf "${AQUA}Network Cards${NC}\n"
network_cards

printf "${AQUA}SCSI Devices${NC}\n"
scsi_devices

printf "${AQUA}Block Devices${NC}\n"
echo "Note: you may identify whether disk is SDD or HDD by ROTA parameter"
echo "1 - HDD, 0 - SSD"
block_devices