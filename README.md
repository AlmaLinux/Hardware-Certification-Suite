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

Create your test directory in the `~/hardware-certification` folder, for example `example`.

Test directory structure:

|- example  
|-- roles  
|--- setup.yml - installation of utilities for testing  
|--- test.yml - test script launch. On failure, the task will not interrupt execution of the following tasks due to the `ignore_errors: yes` setting  
|--- cleanup.yml - remove utilities for testing  
|-- README.md - instructions for working with the test when manually launched  
|-- run_test.sh - script to run the test  

Each test should store test results and utility output in a file `name`_testing.log in the root directory of the repository `~/hardware-certification/logs/`

Each test must be marked with a tag, for example `tags: test_example`

Add your tasks that perform the test to the main.yml file located in the root of the repository.

Add your test settings to the `~/hardware-certification/vars.yml` file if required

How to run test on SUT
===
Run tests on the LTS, comma after IP address is required  
`ansible-playbook -i 192.168.244.7, main.yml`

How to run locally to test play
===
Run command:  
`ansible-playbook -c local -i 127.0.0.1, main.yml`

Variables
===
Tests can be configured via `~/hardware-certification/vars.yml` file.

* lts_ip - LTS IP address
* sut_ip - SUT IP address
* test_cpu['duration'] - stop stress test after T seconds. You can specify time units in seconds, minutes, hours, days, or years with the s, m, h, d, or y suffix. If the timeout is 0, the test will run forever.

Test tags
===
You can run tests by tag

* test_cpu - Run cpu test `ansible-playbook -i 192.168.244.7, main.yml --tags test_cpu`

TIPS
===
Roles work in the context of the SUT, to run a command on the LTS, you need to run commands using `local_action`.

Run command on the LTS: local_action  
Run command on the SUT: command, sh, etc.