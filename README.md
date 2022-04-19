Readme
===
LTS - local testing server
SUT - system under tests

Configure LTS
===
Example SUT IP address: `192.168.244.7`

1. Install Ansible  
`yum --enablerepo=epel install ansible`

2. Add key  
`ssh-keygen -t rsa`

3. Add a key to SUT, a comma after the IP address is required  
`ansible all -i 192.168.244.7, -m authorized_key -a "user=root key='{{ lookup('file', '/root/.ssh/id_rsa.pub') }}' path=/root/.ssh/authorized_keys manage_dir=no" --ask-pass`

4. Check connection with SUT, comma after IP address is required  
`ansible all -i 192.168.244.7, -m ping -u root`

How to add a new test
===

Clone repository
`cd ~ && git clone "https://LOGIN@gerrit.cloudlinux.com/a/hardware-certification"`

Create your test directory in the `~/hardware-certification/tests` folder, for example `example`.

**Test directory structure for automated tests**

|- tests/example  
|-- roles  
|--- main.yml - Ansible tasks  
|-- README.md - instructions for working with the test when manually launched  
|-- run_test.sh - script to run the test

**Test directory structure for interactive tests**

|- tests/example  
|-- step1.yml - sub playbook with interactive prompts  
|-- step2.yml - sub playbook with interactive prompts  
|-- stepx.yml - sub playbook with interactive prompts  
|-- README.md - instructions for working with the test when manually launched

Each automated test should store test results and utility output in a file `name`.log in the root directory of the repository `~/hardware-certification/logs/`. You can get the folder path from a variable `{{ lts_logs_dir }}`.

Add your automated tasks that perform the test to the `~/hardware-certification/automated.yml` file and interactive playbook to the `~/hardware-certification/interactive.yml` file located in the root of the repository.

Each test must be marked with a tag, for example `tags: test_example`

Add your test settings to the `~/hardware-certification/vars.yml` file if required

All tests are always run on LTS. How to run a test on LTS.
===
To run all automated tests:
* Run tests on the LTS, comma after IP address is required  
`ansible-playbook -i 192.168.244.7, automated.yml`

* Run Phoronix tests on the LTS  
`ansible-playbook -i 192.168.244.7, automated.yml --tags phoronix`

To run all interactive tests: Run tests on the LTS, comma after IP address is required  
`ansible-playbook -i 192.168.244.7, interactive.yml`

How to run locally to test play
===
Run command:  
`ansible-playbook -c local -i 127.0.0.1, automated.yml`

Variables
===
Tests can be configured via `~/hardware-certification/vars.yml` file.

* lts_ip - LTS IP address
* lts_tests_dir - LTS test folder
* lts_logs_dir - LTS logs folder
* sut_ip - SUT IP address
* sut_tests_dir - SUT logs folder
* test_cpu['duration'] - stop stress test after T seconds. You can specify time units in seconds, minutes, hours, days, or years with the s, m, h, d, or y suffix. If the timeout is 0, the test will run forever.
* test_network['duration'] - Test duration in seconds
* test_network['speed'] - Target network test speed in Mbps
* test_network['device'] - Testing a specific network device
* test_raid['duration'] - Test duration in seconds
* test_ltp['suites'] - Specify PATTERN to only run test cases which match PATTERN. By default all tests.
* test_phoronix['suites'] - Define test cases
* test_phoronix['folder'] - Specify a folder for installing tests and storing results

Test tags
===
You can run automated tests by tag.
For example:  
`ansible-playbook -i 192.168.244.7, automated.yml --tags cpu`

Available tags:

* logs_folder - create logs folder
* tests_copy - copy `~/hardware-certification/tests` folder from LTS to SUT
* tests_cleanup - remove tests folder from SUT
* containers - test
* cpu - test
* hw_detection - test
* pxe - test
* kvm - test
* network - test
* raid - test
* phoronix - test suits (can only be run by tag)
* ltp - tests (can only be run by tag)
* cllimits - test suits for lve/cagefs checkers (CL only)

Interactive tests can't be run separately.

Results
===
The ansible output will display information about each test. If there are errors, the tests will be colored red.

Summary information will display the test result. Notice the ignored=0 value. If it is > 0, the test has failed.  
The value of failed is always 0, due to skipping failed tests for further sequential execution.  
Example: `127.0.0.1 : ok=9 changed=7 unreachable=0 failed=0 skipped=0 rescued=0 ignored=1`

TIPS
===
 
* Roles work in the context of the SUT, to run a command on the LTS, you need to run commands using `local_action`.    
`Run command on the LTS: local_action`  
`Run command on the SUT: command, sh, etc.` 
* Before starting testing, you need to request information about the hardware. For example, it is not necessary to run a RAID test everywhere.
* Notify in advance of the need to prepare the number of devices equal to the number of USB ports on the server to run the USB test.
* Testing can be delayed, it is recommended to use the screen utility. For example `screen -L -S hctest`
* For phoronix test, you need more than 100 gigabytes of space, by default it installs dependencies in the `/root` folder, to change the section, you need to change the `test_phoronix['folder']` in the `vars.yml` file.