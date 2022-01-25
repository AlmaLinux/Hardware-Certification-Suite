#!/usr/bin/env sh

RED='\033[0;31m'
NC="\033[0m"

function error() {
  printf "${RED}error: $1${NC}\n"
}

echo '========== qemu-installation-checks section start =========='

if [[ ! $(lsmod | grep kvm) ]]; then
  echo 'kernel kvm module is not loaded, trying to modprobe it'
  modprobe kvm
  rc=$?
  if [[ $rc -ne 0 ]]; then
    error 'failed to load kernel kvm module'
    exit $rc
  fi
fi

if [[ $(dmesg | grep -i 'kvm: disabled by bios') ]]; then
  error 'kvm is disabled by BIOS'
  exit 1
fi

if [[ ! $(lsmod | egrep 'kvm_(amd|intel)[^$]') ]]; then
  error 'kvm_amd or kvm_intel module not loaded'
  exit 2
fi

if [[ ! $(ls -la /dev/kvm) ]]; then
  error '/dev/kvm is not found'
  exit 3
fi

echo '========== qemu-installation-checks section end =========='

exit 0
