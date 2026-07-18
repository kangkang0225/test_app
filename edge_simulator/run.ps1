param(
    [string]$Config = "config.json",
    [ValidateSet("web", "interactive", "validate", "provision", "doctor", "scenario", "full")]
    [string]$Command = "web",
    [switch]$VerboseOutput
)

$SimulatorHome = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $SimulatorHome
try {
    $Arguments = @("-m", "edge_simulator", "--config", $Config)
    if ($VerboseOutput) {
        $Arguments += "--verbose"
    }
    $Arguments += $Command
    & python @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
