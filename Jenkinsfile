#!groovy


import java.text.SimpleDateFormat


Map matrix_axes = [:]
common_vm_list = []


CL_VM = [
    '8': 'np_cloudlinux8_x86_64'
]

AL_VM = [
    '8': 'np_almalinux8_x86_64'
]

CL_VMS = CPR_CL_VER.split()
AL_VMS = CPR_AL_VER.split()

for(osver in CL_VMS) {
    common_vm_list += CL_VM[osver]
}

for(osver in AL_VMS) {
    common_vm_list += AL_VM[osver]
}

template_info = [
    'np_cloudlinux8_x86_64': '39001',
    'np_almalinux8_x86_64': '39000'
]

def withSecretEnv(List<Map> varAndPasswordList, Closure closure) {
  wrap([$class: 'MaskPasswordsBuildWrapper', varPasswordPairs: varAndPasswordList]) {
    withEnv(varAndPasswordList.collect { "${it.var}=${it.password}" }) {
      closure()
    }
  }
}

@NonCPS
List getMatrixAxes(Map matrix_axes) {
  List axes = []
  matrix_axes.each { axis, values ->
    List axisList = []
    values.each { value ->
      axisList << [(axis): value]
    }
    axes << axisList
  }
  // calculate cartesian product
  axes.combinations()*.sum()
}

timestamps {
  node('Manage_Jenkins_c-projects') {
    stage ("Clone project") {
      //delete workspace before project cloning:
      cleanWs deleteDirs: true
      checkout(
        [$class: "GitSCM",
          branches: [[name: "${GERRIT_PATCHSET_REVISION}"]],
          doGenerateSubmoduleConfigurations: false,
          extensions: [],
          submoduleCfg: [],
          userRemoteConfigs: [
            [credentialsId: 'ea53ef00-7eba-4f88-98bc-bd77249f49d8',
            refspec: "${GERRIT_REFSPEC}",
            url: "ssh://skozhekin@gerrit.cloudlinux.com:29418/hardware-certification"]
          ]
        ]
      )
    }

    // you can add more axes and this will still work
    println('Use common_vm_list values => ' + common_vm_list)
    matrix_axes = [
      PLATFORM: common_vm_list
    ]
  }

  List axes = getMatrixAxes(matrix_axes)

  // parallel task map
  Map tasks = [failFast: false]

  for(int i = 0; i < axes.size(); i++) {
    // convert the Axis into valid values for withEnv step
    Map axis = axes[i]
    List axisEnv = axis.collect { k, v ->
        "${k}=${v}"
    }
    tasks[axisEnv.join(', ')] = { ->
      withEnv(axisEnv) {
        withSecretEnv([[var: ONE_USER, password: ONE_PASSWORD]]) {
          def list_of_logs = []
          def date = new Date()
          def sdf = new SimpleDateFormat("MM_dd_yyyy-HH.mm.ss")
          def path_to_ansible_host = 'ansible_hosts' + PLATFORM
          def ulf = PLATFORM + 'logs'
          sh '''
          mkdir ''' +ulf+ '''
          '''

          //vm_disk_size: '30'
          Map opennebula_opts = [
            TEMPLATE_ID: template_info[PLATFORM],
            VM_NAME: 'hardware-certification' + '_' + PLATFORM + '_' + sdf.format(date),
            apipassword: ONE_PASSWORD,
            apiurl: ONE_ENDPOINT,
            apiusername: ONE_USER,
            machine_info: PLATFORM
          ]

          Map run_opts = [
            run_on_nebula: 'yes',
            unique_logs_folder: ulf,
            cpu_duration: '1m',
            network_duration: '60',
            network_speed: '1000',
            raid_duration: '60',
            phoronix_suites: 'ebizzy && echo "success"',
            phoronix_folder: '/root',
            phoronix_need_space: '1',
            ltp_suites: 'fork'
          ]

          stage("Create VM ${PLATFORM}") {
            //add key 'what action we want to do'; crt == create (vm); rmvm == remove (vm)
            opennebula_opts.put('one_act', 'crt')
            dir("${env.WORKSPACE}") {
              //create vm
              ansiblePlaybook playbook: 'opennebula.yml', extraVars: opennebula_opts
            }
          }

          stage("Run tests ${PLATFORM}") {
            dir("${env.WORKSPACE}") {
              ansiblePlaybook playbook: 'automated.yml', inventory: path_to_ansible_host, extraVars: run_opts
            }

            dir("${env.WORKSPACE}") {
              ansiblePlaybook playbook: 'automated.yml', inventory: path_to_ansible_host, extraVars: run_opts, tags: 'ltp'
            }

            dir("${env.WORKSPACE}") {
              ansiblePlaybook playbook: 'automated.yml', inventory: path_to_ansible_host, extraVars: run_opts, tags: 'phoronix'
            }

            // collects all appeared log names
            dir("${env.WORKSPACE}/${ulf}") {
              def files = findFiles(glob: '*.log')
              files.each {
                list_of_logs += it.name
              }
            }
          }

          // Generates stages so that each log be printed in a separate window
          list_of_logs.each {
            stage("Show results ${it} ${PLATFORM}") {
              dir("${env.WORKSPACE}/${ulf}") {
                def log_output = readFile(file: it)
                println(log_output)
                // It's impossible to check the results for logs from
                // the list below at the moment so the <success check>
                // has been skipped for them
                def success_is_not_ensured = ['hw_detection.log',
                                              'pxe.log',
                                              'kvm.log']
                if(!success_is_not_ensured.contains(it)) {
                  catchError(message: 'Test failed', buildResult: 'UNSTABLE', stageResult: 'FAILURE') {
                    if(it == 'containers.log') {
                      assert log_output.findAll('Success, docker network is operating normally')
                    } else {
                      assert log_output[-20..-1].toLowerCase().findAll('success')
                    }
                  }
                }
              }
            }
          }

          stage("Delete VM ${PLATFORM}") {
            dir("${env.WORKSPACE}") {
              //delete vm
              opennebula_opts.put('one_act', 'rmvm')
              catchError(message: 'Unable to delete the VM', buildResult: 'SUCCESS', stageResult: 'FAILURE') {
                ansiblePlaybook playbook: 'opennebula.yml', extraVars: opennebula_opts
              }
            }
          }
        }
      }
    }
  }

  node('Manage_Jenkins_c-projects') {
    stage("Matrix builds") {
      timeout(time: 240, unit: 'MINUTES') {
        parallel(tasks)
      }
    }
    //delete workspace at the end:
    cleanWs deleteDirs: true
  }
}
