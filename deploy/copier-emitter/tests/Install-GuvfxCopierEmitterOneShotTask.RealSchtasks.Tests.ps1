#Requires -Version 5.1
# Real-schtasks regression for the one-shot installer's native helper (GFX-COPIER-ONESHOT-INSTALLER-NATIVE-SCHTASKS-
# FIX-103). Runs ONLY on a Windows runner with a real schtasks.exe (there is no Windows in Linux CI; the mocked suite
# in Install-GuvfxCopierEmitterOneShotTask.Tests.ps1 covers the fake path). It proves the PS 5.1 native-stderr defect
# (GFX-COPIER-PARITY-VALIDATOR-FIX-013) is gone: an ABSENT-task /query under $ErrorActionPreference='Stop' must NOT
# throw, must return the native exit code (never a terminating error), must be classified absent, and must create no
# task - plus a RULE 11 POSCONTROL proving the SAME measurement path can find a task that DOES exist (so "not found"
# is a real negative, not a broken parser). Non-production ONLY: synthetic collision-resistant task names, no real
# account, no secret, no host/backend/broker, and defensive finally cleanup of every task it touches.

BeforeAll {
    $script:Installer = Join-Path (Split-Path $PSScriptRoot -Parent) 'Install-GuvfxCopierEmitterOneShotTask.ps1'
    . $script:Installer   # dot-source: defines Invoke-GuvfxSchtasksNative (+ the installer fn); does NOT execute

    # clearly non-production, collision-resistant names that can never equal a real GuvFX task (distinct prefix + GUID)
    $script:AbsentTask = 'GuvFxPesterAbsent_' + ([guid]::NewGuid().ToString('N'))
    $script:PosTask    = 'GuvFxPesterPosCtrl_' + ([guid]::NewGuid().ToString('N'))

    # cleanup that dogfoods the safe helper: it must itself never throw when the task is absent
    function Remove-TestTaskQuiet([string]$name) {
        $null = Invoke-GuvfxSchtasksNative -ArgList @('/delete', '/tn', ('\' + $name), '/f')
    }
}

AfterAll {
    Remove-TestTaskQuiet $script:AbsentTask
    Remove-TestTaskQuiet $script:PosTask
}

Describe 'Invoke-GuvfxSchtasksNative - real schtasks absent-task path (PS 5.1 native-stderr regression)' {

    It 'an absent-task /query under EAP=Stop does NOT throw and returns a captured non-zero exit' {
        $ErrorActionPreference = 'Stop'                       # reproduce the EXACT trap condition
        Remove-TestTaskQuiet $script:AbsentTask               # ensure truly absent
        $threw = $false
        $res = $null
        try { $res = Invoke-GuvfxSchtasksNative -ArgList @('/query', '/tn', ('\' + $script:AbsentTask), '/xml', 'ONE') }
        catch { $threw = $true }
        $threw | Should -BeFalse                              # the old `2> null` default WOULD have thrown here
        $res | Should -Not -BeNullOrEmpty
        $res.ExitCode | Should -Not -Be 0                     # absent -> non-zero native exit, CAPTURED not thrown
    }

    It 'classifies the absent task exactly as the installer Read-TaskXml does (null)' {
        $ErrorActionPreference = 'Stop'
        $res = Invoke-GuvfxSchtasksNative -ArgList @('/query', '/tn', ('\' + $script:AbsentTask), '/xml', 'ONE')
        $absent = ($null -eq $res -or $res.ExitCode -ne 0 -or -not $res.Out)
        $absent | Should -BeTrue
    }

    It 'restores the caller ErrorActionPreference after the absent-task query' {
        $ErrorActionPreference = 'Stop'
        $before = $ErrorActionPreference
        $null = Invoke-GuvfxSchtasksNative -ArgList @('/query', '/tn', ('\' + $script:AbsentTask), '/xml', 'ONE')
        $ErrorActionPreference | Should -BeExactly $before    # the helper must not leak EAP=Continue to the caller
    }

    It 'the absent-task /query is read-only (creates no task)' {
        Remove-TestTaskQuiet $script:AbsentTask
        $null = Invoke-GuvfxSchtasksNative -ArgList @('/query', '/tn', ('\' + $script:AbsentTask), '/xml', 'ONE')
        $check = Invoke-GuvfxSchtasksNative -ArgList @('/query', '/tn', ('\' + $script:AbsentTask), '/xml', 'ONE')
        $check.ExitCode | Should -Not -Be 0                   # still absent after the query
    }

    It 'POSCONTROL (RULE 11): the same helper path finds a task that DOES exist (exit 0 + XML)' {
        try {
            $c = Invoke-GuvfxSchtasksNative -ArgList @('/create', '/tn', ('\' + $script:PosTask),
                '/tr', 'cmd.exe /c exit 0', '/sc', 'ONCE', '/st', '23:59', '/f')
            $c.ExitCode | Should -Be 0                        # the measurement path can create
            $q = Invoke-GuvfxSchtasksNative -ArgList @('/query', '/tn', ('\' + $script:PosTask), '/xml', 'ONE')
            $q.ExitCode | Should -Be 0                        # present -> zero exit (positive control)
            $q.Out | Should -Match '<Task'                    # real task XML returned
        }
        finally {
            Remove-TestTaskQuiet $script:PosTask              # never leave the synthetic task behind
        }
    }
}
