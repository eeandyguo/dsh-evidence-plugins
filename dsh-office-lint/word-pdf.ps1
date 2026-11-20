param(
  [Parameter(Mandatory = $true)][string]$Source,
  [Parameter(Mandatory = $true)][string]$Pdf
)

# Export a .docx to PDF with Microsoft Word, read-only, no UI.
# Invoked by the Host plugin through 32-bit Windows PowerShell: the 32-bit
# registry is where Office registers its Word.Application, while the 64-bit
# ProgID on some machines belongs to WPS Office.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

# Never touch a Word session the user already has open.
if (Get-Process WINWORD -ErrorAction SilentlyContinue) {
  Write-Output 'BUSY Word is already running'
  exit 3
}

$seed = $null
$word = $null
$doc = $null
try {
  # Office16 exposes the document class below; its .Application is real Word
  # even when a WPS install has taken over the Word.Application ProgID.
  try {
    $seed = [Activator]::CreateInstance([Type]::GetTypeFromCLSID([Guid]'{1B261B22-AC6A-4E68-A870-AB5080E8687B}'))
    $word = $seed.Application
  } catch {
    $word = $null
  }
  if ($word -eq $null) {
    $candidate = New-Object -ComObject Word.Application
    $candidatePath = ''
    try { $candidatePath = $candidate.Path } catch {}
    if ($candidatePath -like '*Kingsoft*') {
      try { $candidate.Quit() } catch {}
      throw 'Word.Application is owned by WPS, not Microsoft Word'
    }
    $word = $candidate
  }

  $word.Visible = $false
  $word.DisplayAlerts = 0
  $word.AutomationSecurity = 3
  $doc = $word.Documents.Open($Source, $false, $true)
  $doc.Repaginate()
  $pages = $doc.ComputeStatistics(2)
  $doc.ExportAsFixedFormat($Pdf, 17)
  if (-not (Test-Path -LiteralPath $Pdf)) { throw 'Word did not produce a PDF' }
  Write-Output ('OK pages=' + $pages + ' bytes=' + (Get-Item -LiteralPath $Pdf).Length)
} catch {
  Write-Output ('FAIL ' + $_.Exception.Message)
  exit 1
} finally {
  # Close(0)/Quit(0) = wdDoNotSaveChanges: never prompt, never keep a dialog
  # alive. COM references are released when this PowerShell process exits;
  # explicit ReleaseComObject calls can block on a server that already exited.
  if ($doc -ne $null) { try { $doc.Close(0) } catch {} }
  if ($seed -ne $null) { try { $seed.Close(0) } catch {} }
  if ($word -ne $null) { try { $word.Quit(0) } catch {} }
}
