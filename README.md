# AlmaLinux Certification Suite
This repo is the home of the AlmaLinux Certification Suite.  We largely rely on open source utilities, tests, and benchmarks to ensure various types workloads are stable on a given hardware configuration.

# Terminology
- LTS - Local Testing Server
  - This is the server running the ansible-playbook
- SUT - System Under Tests
  - This is the system being tested

# Common Requirements
- SUT should be a blank, freshly installed and updated AlmaLinux system.
- ansible >= 2.17 (older may work, but is untested)
- git
- screen, tmux, or local shell access
- \>= 300GB disk space, preferably SSD/NVMe

# Suggested Run

## Local Run
The suggested way to run the certification suite is combining LTS/SUT - this means running this ansible playbook from the same host that is being tested.  This avoids any network-related issues between LTS and SUT causing a failure.  The expected runtime of the playbook is around 48 hours so the chance of a network blip causing a failure over publicly-networked hosts is great.

We recommend using a local console or in something like `screen` or `tmux`.  We will use `tmux` in this example.

  - Install git-core, tmux, and Python 3.12
    ```bash
    dnf install git-core tmux python3.12 -y
    ```
  - Clone this repository
    ```bash
    git clone https://github.com/AlmaLinux/Hardware-Certification-Suite.git
    ```
  - Create python venv  with updated ansible (versus ansible version in EPEL)
    ```bash
    # create venv
    python3.12 -m venv venv
    # activate venv
    source venv/bin/activate
    # install ansible
    pip install ansible
    ```
  - Run test suite in tmux session
    ```bash
    # move into ansible playbook dir
    cd Hardware-Certification-Suite
    # start tmux session
    tmux new-session -s almalinux-certification-tests
    # run playbook
    ansible-playbook -c local -i 127.0.0.1, automated.yml --tags=phoronix
    ```

## Remote LTS/SUT
### Configure LTS
#### Requirements
- ansible >= 2.17
- tmux

#### Setup Commands
##### AlmaLinux
```bash
# install python 3.12
dnf -y install python3.12 tmux
# create venv
python3.12 -m venv venv-almalinux-certification-suite
# activate venv
source venv-almalinux-certification-suite/bin/activate
# install ansible
pip install ansible
# start tmux session
tmux new-session -s almalinux-certification-tests
# run playbook
ansible-playbook -i <SUT IP>, automated.yml --tags phoronix
```

##### Fedora >= 40
```bash
# install ansible and tmux
dnf -y install ansible tmux
# start tmux session
tmux new-session -s almalinux-certification-tests
# run playbook
ansible-playbook -i <SUT IP>, automated.yml --tags phoronix
```

# Advanced information
=======
# Hardware Certification Suite
This repo is the home of the AlmaLinux Certification Suite, built and maintained by the [AlmaLinux Certification SIG](https://wiki.almalinux.org/sigs/Certification). Contributions to this suite are welcome, and we invite contributors to become active in the SIG itself. 

## Extensions - hardware- or software-specific tests

The certification suite is built modularly with intention, and we would love to expand this suite as our community needs to include the creation of hardware- or software-vendor specific test(s), and running them on request. 

### Example

For MariaDB database we could include a mariadb-test runner with parameters defined by MariaDB Foundation team for the suite to be able to detect functional regressions (potentially even including performance tests).

# Running the Certification suite

Below describes how to run the suite itself. Once the suite is run, results should be submitted to the (Certifications repo)[https://github.com/AlmaLinux/certifications]. 

===
Definitions: 
* LTS - local testing server
* SUT - system under tests

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
`cd ~ && git clone "https://github.com/AlmaLinux/Hardware-Certification-Suite.git"`

Create your test directory in the `~/Hardware-Certification-Suite/tests` folder, for example `example`.

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

Each automated test should store test results and utility output in a file `name`.log in the root directory of the repository `~/Hardware-Certification-Suite/logs/`. You can get the folder path from a variable `{{ lts_logs_dir }}`.

Add your automated tasks that perform the test to the `~/Hardware-Certification-Suite/automated.yml` file and interactive playbook to the `~/Hardware-Certification-Suite/interactive.yml` file located in the root of the repository.

Each test must be marked with a tag, for example `tags: test_example`

Add your test settings to the `~/Hardware-Certification-Suite/vars.yml` file if required

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
Tests can be configured via `~/Hardware-Certification-Suite/vars.yml` file.

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
* tests_copy - copy `~/Hardware-Certification-Suite/tests` folder from LTS to SUT
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

---
This repo is managed by the [AlmaLinux Certification SIG](https://wiki.almalinux.org/sigs/Certification)
