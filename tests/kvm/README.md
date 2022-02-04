## KVM compatibility test

### What the test covers
The test is designed to check possibility to run virtual machines with KVM.

### What the test does
The test performs the following actions:
1. Checks that CPU provides virtualization features
2. Installs needed packages: qemu-kvm, libvirt
3. Creates and launches a minimal VM
4. Checks that libvirt's default virtual network is working
5. Checks that VM is working
6. Cleanups VMs and frees resources
7. Uninstalls previously installed packages: qemu-kvm, libvirt

### Executing the test
The test is non-interactive.

To execute in manual mode do following:
1. Run `pre-checks.sh` and check the return code - it should be 0, otherwise other steps are meaningless
2. Execute `yum install qemu-kvm libvirt`
3. Execute `systemctl restart libvirtd`
4. Make a symlink `/usr/bin/qemu-kvm` --> `/usr/libexec/qemu-kvm`
5. Run `qemu-installation-checks.sh` and check the return code - 0 means that qemu installed correctly
6. Run `run_test.sh`

Exit code 0 means that test succeeded, all other exit codes mean error.

The test is multistage - if `pre-checks` or `qemu-installation-checks` script fails, the rest part of test will not run.
So you can execute `grep 'Test passed' kvm_compatibility.log` to check success.

### Preparing for the test
Requirements:
* Internet connection
* ~500Mb of disk space to install needed packages (qemu-kvm, libvirt)
* 16Mb RAM available - these resources will be provided to the VM

### Run time
The test takes around 1 minute to run (exclude packages download time)
