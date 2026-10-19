pipeline {
    agent any

    stages{

        stage('Scan Trivy') {
            steps {
                sh '''
                    
                    BASE="$HOME/.jenkins-tools"
                    mkdir -p "$BASE/bin" "$BASE/cache/trivy" "$BASE/reports"

                    # Installer Trivy localement dans $HOME si absent
                    if [ ! -x "$BASE/bin/trivy" ]; then
                        echo "Installation locale de Trivy dans \$BASE/bin ..."
                        TMP_DIR="$(mktemp -d)"
                        cd "$TMP_DIR"
                        curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh \
                        | sh -s -- -b "$BASE/bin"
                        cd -
                        rm -rf "$TMP_DIR"
                    fi

                    export PATH="$BASE/bin:$PATH"
                    export TRIVY_CACHE_DIR="$BASE/cache/trivy"

                    echo "=== Scan Trivy ==="
                    trivy fs \
                        --format sarif \
                        --scanners vuln,secret,misconfig \
                        --no-progress \
                        --output "$BASE/reports/trivy.sarif" \
                        "${WORKSPACE}"

                '''
            }
        }

        stage('Gitleaks') {
            steps {
                sh '''

                BASE="$HOME/.jenkins-tools"
                BIN="$BASE/bin"
                REPO="$BASE/reports"

                mkdir -p "$BIN" "$REPO"

                echo "=== Installation GitLeaks dans $BIN ==="

                if [ ! -x "$BIN/gitleaks" ]; then

                    TMP_DIR="$(mktemp -d)"
                    cd "$TMP_DIR"

                    # Récupère l'URL de l'asset linux_x64
                    ASSET_URL=$(curl -s https://api.github.com/repos/gitleaks/gitleaks/releases/latest \
                        | grep browser_download_url \
                        | grep linux_x64.tar.gz \
                        | cut -d '"' -f 4)

                    if [ -z "$ASSET_URL" ]; then
                        echo "Impossible de trouver l'asset linux_x64.tar.gz"
                        exit 1
                    fi

                    curl -fSL "$ASSET_URL" -o gitleaks.tar.gz

                    tar -xzf gitleaks.tar.gz

                    BINFILE=$(find . -type f -name gitleaks | head -n 1)


                    chmod +x "$BINFILE"
                    mv "$BINFILE" "$BIN/gitleaks"

                    cd -
                    rm -rf "$TMP_DIR"
                fi


                export PATH="$BIN:$PATH"
                
                gitleaks detect \
                --source "${WORKSPACE}" \
                --no-git \
                --redact \
                --config .gitleaks.toml \
                --report-format sarif \
                --report-path "$REPO/gitleaks.sarif" \
                --exit-code 0 \
                -v
                '''
            }
        }

        stage('Scan OSV-Scanner') {
            steps {
                sh '''

                BASE="$HOME/.jenkins-tools"
                BIN="$BASE/bin"
                CACHE="$BASE/cache/osv"
                REPO="$BASE/reports"

                mkdir -p "$BIN" "$CACHE" "$REPO"

                #=== Installation OSV-Scanner dans $BIN ===

                if [ ! -x "$BIN/osv-scanner" ]; then
                    TMP="$(mktemp -d)"
                    cd "$TMP"

                    curl -fSL \
                    https://github.com/google/osv-scanner/releases/latest/download/osv-scanner_linux_amd64 \
                    -o osv-scanner

                    chmod +x osv-scanner
                    mv osv-scanner "$BIN/osv-scanner"

                    cd -
                    rm -rf "$TMP"
                fi

                export PATH="$BIN:$PATH"
                export OSV_SCANNER_CACHE="$CACHE"

                osv-scanner scan \
                    --offline \
                    --download-offline-databases \
                    -r "${WORKSPACE}" \
                    --format sarif \
                    > "$REPO/osv.sarif" || true

                '''
            }
        }


    //#################################################################
    //# AUTORISER FIREWALL AVANT DE L'UTILISER SINON ECHEC DU PIPELINE# 
    //#################################################################

    //     stage('semgrep') {
    //        steps {
    //            sh '''
    //             BASE="$HOME/.jenkins-tools"
    //             REPO="$BASE/reports"
    //             mkdir -p "$REPO"


    //             if ! command -v pip3 >/dev/null 2>&1; then
    //                 echo "pip absent -> installation via get-pip.py"
    //                 curl -sS https://bootstrap.pypa.io/get-pip.py -o get-pip.py
    //                 python3 get-pip.py --user
    //             fi

    //             export PATH="$HOME/.local/bin:$PATH"
                
    //             pip3 install --user --upgrade semgrep

    //             semgrep --config auto --sarif --output "$REPO/semgrep.sarif" .
    //           '''
    //         }
    //    }

        stage('Checkov') {
            steps {
                sh '''
                    BASE="$HOME/.jenkins-tools"
                    REPO="$BASE/reports"
                    mkdir -p "$REPO"


                    if ! command -v pip >/dev/null 2>&1; then
                        echo "pip absent : installation via get-pip.py"
                        curl -sS https://bootstrap.pypa.io/pip/3.9/get-pip.py -o get-pip.py
                        python3 get-pip.py --user
                    fi

                    export PATH="$HOME/.local/bin:$PATH"

                    pip3 install --user --upgrade checkov

                    checkov -d . -o sarif --output-file result.sarif --quiet --soft-fail

                    mv result.sarif/* "$REPO/checkov.sarif"
                '''
            }
        }

        stage('SonarQube') {
            steps {
                script {
                    def scannerHome = tool 'SonarScanner'

                    withSonarQubeEnv('SonarQube') {
                        
                            sh """
                                echo '=== Construction liste SARIF ==='
                                SARIFS=\$(ls $HOME/.jenkins-tools/reports/*.sarif 2>/dev/null | paste -sd, - || true)
                                echo "SARIFS = \$SARIFS"

                                echo '=== Lancement du scanner SonarQube ==='
                                "${scannerHome}/bin/sonar-scanner" \
                                    -Dsonar.projectKey=tst_protect  \
                                    -Dsonar.sources=. \
                                    -Dsonar.host.url=https://aism-apsnqx01-o/ \
                                    -Dsonar.token=sqp_294d1ff06980afc6202aba6873f11b0ab4463813 \
                                    -Dsonar.sarifReportPaths="\$SARIFS"
                            """
                        
                    }
                }
            }
        }

        

        stage('Move SARIF to workspace') {
            steps {
                sh '''
                mkdir -p "$WORKSPACE/reports"
                mv "$HOME/.jenkins-tools/reports/"*.sarif "$WORKSPACE/reports/" || true
                '''
            }
        }

        stage('Validation Sécurité') {
            steps {
                script {
                    def high = sh(
                        script: '''
                            if ! ls reports/*.sarif >/dev/null 2>&1; then
                                echo 0
                                exit 0
                            fi

                            sca=$(grep -ho '"security-severity"[[:space:]]*:[[:space:]]*"[^"]*"' reports/*.sarif \
                                | awk -F'"' '$4 >= 7 {c++} END {print c+0}')

                            misconf=$(grep -ho '"level"[[:space:]]*:[[:space:]]*"error"' reports/*.sarif | wc -l)

                            echo $((sca + misconf))
                        ''',
                        returnStdout: true
                    ).trim()


                    if (high.toInteger() > 0) {
                        currentBuild.result = 'UNSTABLE'
                        env.SECURITY_HIGH = high
                    }
                }
            }
        }


        stage('Publish SARIF') {
            steps {
                recordIssues enabledForFailure: true, tools: [sarif(pattern: "reports/*.sarif")]
            }
        }

        stage('Clean Workspace'){
            steps{
                sh'''
                    rm -rf reports/*
                '''
            }
        }

        stage('Quality Gate') {
            steps{
                timeout(time: 1, unit: 'HOURS'){
                    waitForQualityGate abortPipeline: true
                }
            }
        }

        stage('Stability Check') {
            steps {
                script {

                    if (currentBuild.currentResult == 'UNSTABLE') {
                        error("FAIL : vulnérabilités HIGH/CRITICAL détectées dans les rapports SARIF (${env.SECURITY_HIGH})")
                    }
                }
            }
        }



    }
}
