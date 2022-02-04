#!/usr/bin/bash

set +ex

if [ $# -lt 2 ]; then
	echo "usage: $0 <LTS-host> <SUT-host> [test-time-in-minuts] [target-network-speed]"
	exit 1
fi

LTS_HOST=$1
SUT_HOST=$2
TEST_TIME=${3-14400}
TARGET_SPEED=${4-false}

ALLOWED_PERCENTAGE=0.8
global_result=true

if [ $TEST_TIME -lt 5 ]; then
    TEST_TIME=5
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC="\033[0m"

function error() {
    printf "${RED}[$(date)] $1${NC}\n"
}
function success() {
    printf "${GREEN}[$(date)] $1${NC}\n"
}
function info() {
    printf "${BLUE}[$(date)] $1${NC}\n"
}

sut_command() {
    ssh -o StrictHostKeyChecking=no root@$SUT_HOST "$1"
    return_code=$?
    if [ $return_code -gt 0 ] && [ "$2" != "true" ]; then
        error "Failed to execution SUT command: $1"
        error "Return code: $return_code"
        error "SUT connection error. Test FAILED!"
        global_result=false
    elif [ $return_code -gt 0 ]; then
        return 1
    fi
}

iperf3_test() {
    # Clearing
    info "Clearing environment..."
    sut_command "rm -rf /tmp/stress_testing_network_iperf3_client.log"
    killall iperf3 > /dev/null 2>&1
    sut_command 'killall iperf3 > /dev/null 2>&1' true
    info "Clearing complete"
    # End clearing

    # Stopping firewall
    service firewalld stop > /dev/null 2>&1
    sut_command 'service firewalld stop > /dev/null 2>&1'

    if [ "$TARGET_SPEED" != "false" ] && [ "$TARGET_SPEED" -gt 0 ]; then
        device_speed=$TARGET_SPEED
    else
        device_speed=$(sut_command "ethtool $1 | grep -oP '\d+(?=baseT)' | tail -1")
        if [ "$device_speed" == "Unknown" ] || [ "$device_speed" == "" ]; then
            # Debug mode. Need to fail test if speed not defined
            error "Device speed detection error. Test aborted! See: $ ethtool $1"
            global_result=false
        fi
    fi

    iperf3_server_pid=$(nohup iperf3 -s -p 3000 >> /tmp/stress_testing_network_iperf3_server.log 2>&1 & echo $!)

    if [ $iperf3_server_pid -gt 0 ]; then
        info "LTS Server running. Pid: $iperf3_server_pid"
    fi

    # Start test
    if [ "$global_result" == "true" ]; then
        info "Connecting to $LTS_HOST:3000..."

        iperf3_client_pid=$(sut_command 'nohup iperf3 --client '"$LTS_HOST"'\
        --port 3000 --bitrate '"$device_speed"'M --interval 60\
        --format m --time '"$TEST_TIME"' > /tmp/stress_testing_network_iperf3_client.log 2>&1 & echo $!')

        sleep 1

        sut_command 'kill -0 '"$iperf3_client_pid" true
        if [ $? -gt 0 ]; then
            error "Error connecting to iperf3 server!"
            global_result=false
        else
            info "Connected. Test in progress..."
        fi

        # Progress icon
        spin='-\|/'
        icon=0
        while sut_command 'kill -0 '"$iperf3_client_pid"' 2>/dev/null' true
        do
            icon=$(( (icon+1) %4 ))
            printf "\r${spin:$icon:1}"
            sleep 0.1
        done
        printf "\n"
        # End progress icon

        device_result=($(sut_command "cat /tmp/stress_testing_network_iperf3_client.log | grep -oP '\d+\.\d+ (?=Mbits/sec)' &2>/dev/null"))
        printf "[$(date)]\n|----------------------------------------------------------------|\n"
        success "$(sut_command 'cat /tmp/stress_testing_network_iperf3_client.log')"
        printf "|----------------------------------------------------------------|\n"

        info "killing iperf3 server..."
        kill $iperf3_server_pid
        if [ $? -gt 0 ]; then
            error "Error killing iperf3 server!"
            global_result=false
        else
            info "Iperf3 server successfully stopped"
        fi
    fi
}

info "Starting new test..."

# Check locales
if [ "$(echo $LANG | grep -oP '\w{2}(?=_)')" != "en" ] || [ "$(sut_command "echo $LANG | grep -oP '\w{2}(?=_)'")" != "en" ]; then
    error "Your system language is not English! Test aborted!"
    info "LTS lang=$LANG"
    info "SUT lang=$(sut_command 'echo $LANG')"
    global_result=false
fi

# Catching network devices
network_devices=( $(sut_command "lshw -class network | grep -oP 'logical name: (\w+)' | sed 's/logical name: //'") )

if [ "$global_result" == "true" ]; then
    for i in ${!network_devices[@]}; do
        # switching devices
        info "Switching devices..."
        for device_name in ${network_devices[@]}; do
            if [ "$device_name" != ${network_devices[$i]} ]; then
                info "Device $device_name is down"
                sut_command 'ifdown '"$device_name"' > /dev/null 2>&1'
            else
                info "Device $device_name is up"
                sut_command 'ifup '"$device_name"' > /dev/null 2>&1'
            fi
        done

        # start test for device
        info "Start testing device #$i"
        info "Device name: ${network_devices[$i]}"
        iperf3_test ${network_devices[$i]}

        # Calculate results

        # device_result array
        # Last 2 elements:
        # sender (SUT) bitrate
        # reciever (LTS) bitrate

        # Unset reciever (LTS) bitrate value
        if [ ${#device_result[@]} -gt 0 ]; then
            unset device_result[$((${#device_result[@]}-1))]

            # Calculate allowed speed reduction
            device_allowed_speed=$(echo $device_speed*$ALLOWED_PERCENTAGE | bc)

            # Check all results
            device_test_status=true
            for el in ${device_result[@]}; do
                if [ 1 -eq "$(echo "${el} < ${device_allowed_speed}" | bc)" ]; then
                    device_test_status=false
                    global_result=false
                    error "Device speed to low: ${el}Mbits < ${device_allowed_speed}Mbits"
                fi
            done
        fi
    done

    # Enabling devices
    info "Enabling devices..."
    for device_name in ${network_devices[@]}; do
        sut_command 'ifup '"$device_name"' > /dev/null 2>&1 &'
    done

    # Starting firewall
    service firewalld start > /dev/null 2>&1
    sut_command 'service firewalld start > /dev/null 2>&1 &'
fi

info "For more information see: logs/network_testing.log"

if [ "$global_result" == "true" ]; then
    success 'Test status: SUCCESS!'
    exit 0
else
    error 'Test status: FAILED!'
    exit 1
fi