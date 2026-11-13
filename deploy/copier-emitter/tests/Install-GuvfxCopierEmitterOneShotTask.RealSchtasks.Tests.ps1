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

Describe 'Resolve-GuvfxPrincipalSid - real Windows SID normalization (packet 108)' {
    # The live rollout proved schtasks stores a task principal as a SID, so the APPLY read-back must compare by
    # canonical SID, not by the submitted bare name. These tests exercise the REAL resolver + a REAL schtasks task on a
    # Windows runner (the mock returns the submitted name and never normalizes to a SID). Non-production: the task runs
    # as the runner's CURRENT user (no password, InteractiveToken), a collision-resistant name, disabled+triggerless,
    # cleaned up in finally. No real account/secret/host/broker.
    BeforeAll {
        $script:SidTask = 'GuvFxPesterSid_' + ([guid]::NewGuid().ToString('N'))
        $cur = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $script:CurSid = $cur.User.Value
        $script:CurName = $cur.Name            # DOMAIN\user or MACHINE\user
        $script:CurBare = ($script:CurName -split '\\')[-1]
    }
    AfterAll { Remove-TestTaskQuiet $script:SidTask }

    It 'the real resolver canonicalizes bare name, qualified name and SID of the SAME account to one SID' {
        (Resolve-GuvfxPrincipalSid -Reference $script:CurName) | Should -BeExactly $script:CurSid
        (Resolve-GuvfxPrincipalSid -Reference $script:CurSid)  | Should -BeExactly $script:CurSid
        (Resolve-GuvfxPrincipalSid -Reference $script:CurBare) | Should -BeExactly $script:CurSid
    }

    It 'the real resolver FAILS CLOSED on a dangling SID and a non-existent name (returns null)' {
        (Resolve-GuvfxPrincipalSid -Reference 'S-1-5-21-0-0-0-99999') | Should -BeNullOrEmpty
        (Resolve-GuvfxPrincipalSid -Reference ('NoSuchUser_' + [guid]::NewGuid().ToString('N'))) | Should -BeNullOrEmpty
    }

    It 'a DIFFERENT real identity resolves to a DIFFERENT SID (LocalSystem != current user)' {
        $sys = Resolve-GuvfxPrincipalSid -Reference 'S-1-5-18'    # LocalSystem, a real well-known SID
        $sys | Should -BeExactly 'S-1-5-18'
        $sys | Should -Not -BeExactly $script:CurSid
    }

    It 'a REAL schtasks task read-back returns a SID that canonicalizes to the submitted principal' {
        $tmp = Join-Path $env:TEMP ($script:SidTask + '.xml')
        try {
            $xml = @'
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Settings><Enabled>false</Enabled><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy></Settings>
  <Triggers />
  <Principals><Principal id="Author"><UserId>__USER__</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Actions Context="Author"><Exec><Command>cmd.exe</Command><Arguments>/c exit 0</Arguments></Exec></Actions>
</Task>
'@
            $xml = $xml.Replace('__USER__', $script:CurName)
            Set-Content -LiteralPath $tmp -Value $xml -Encoding Unicode
            $c = Invoke-GuvfxSchtasksNative -ArgList @('/create', '/tn', ('\' + $script:SidTask), '/xml', $tmp, '/f')
            $c.ExitCode | Should -Be 0
            $q = Invoke-GuvfxSchtasksNative -ArgList @('/query', '/tn', ('\' + $script:SidTask), '/xml', 'ONE')
            $q.ExitCode | Should -Be 0
            $rx = [xml]$q.Out
            $returnedUid = ('' + $rx.Task.Principals.Principal.UserId)
            # schtasks normalizes the principal; the resolver must canonicalize the returned value to the same SID as
            # the submitted name -- this is exactly the account-46 read-back the live rollout mishandled.
            (Resolve-GuvfxPrincipalSid -Reference $returnedUid) | Should -BeExactly $script:CurSid
            (Resolve-GuvfxPrincipalSid -Reference $returnedUid) | Should -BeExactly (Resolve-GuvfxPrincipalSid -Reference $script:CurName)
            # and the task was stored DISABLED + triggerless
            ('' + $rx.Task.Settings.Enabled) | Should -BeExactly 'false'
            $tc = 0; if ($rx.Task.Triggers -and $rx.Task.Triggers.ChildNodes) { $tc = [int]$rx.Task.Triggers.ChildNodes.Count }
            $tc | Should -Be 0
        }
        finally {
            Remove-TestTaskQuiet $script:SidTask
            if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
        }
    }
}
