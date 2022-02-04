## USB testing
### Test algorithm
Interactive test.
1. Unplug all devices from USB ports excludes the keyboard.
2. Input number of free USB ports that need to test.
3. Used lsusb tool to define the initial number of devices.
4. Next when prompted plug-in USB devices in all test ports.
5. Used lsusb tool to define the current number of devices.
6. If initial number of devices + inputed number = current number of devices test passed.

### Running test
1. On LTS execute: ansible-playbook -i <SUT_IP>, -u root interactive.yml
### StdOut example
TASK [Result] 

***

fatal: [10.51.0.5]: FAILED! => {"changed": false, "msg": "Expected 4 devices, detected 1! Test Failed!"}

PLAY RECAP 

***

10.51.0.5                  : ok=11   changed=6    unreachable=0    failed=1    skipped=2    rescued=0    ignored=0 