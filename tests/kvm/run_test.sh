#!/usr/bin/env sh
set -e

XML_FILE=${1-"test-vm.xml"}

echo '========== run_test section start =========='

virsh net-list --all | grep default
if [[ ! $(virsh net-list | grep default | grep active) ]]; then
  virsh net-start default
fi
virsh net-list | grep default | grep active

virsh create $XML_FILE

set +e
count=0
while [[ ! $(virsh list | grep test_vm | grep running) && $count -lt 30 ]]; do
  (( count++ ))
  sleep 2
done

if [[ $count -lt 30 ]]; then
  echo "Test passed"
fi
virsh destroy test_vm

echo '========== run_test section end =========='

exit 0
