# Containerization
## What the test covers
The containerization test verifies the ability of a system to run 
processes with isolation inside containers (docker, podman, etc).

## What the test does
The test has multiple phases.

On the first stage, test runs a simple http web server and verifies 
that network proxying works as expected.

On the second stage, test creates a virtual network and assigns 
it to multiple containers verifying that they can talk to each other 
within the internal network.


## Preparing for the test
Ensure that the system is connected to the network before running the test. 
All parameters and required packages will be automatically set by the test server.

## Executing the test
Run
```
cd containers && ./run-test.sh
```

Exit code 0 means that test succeeded, all other exit codes mean error.

Debug logs will be placed into containers.debug.log.


## Run time
The containerization test run time is highly variable. 
It is dependent on the network and storage speed, but the average time is XX mins.

