param(
  [Parameter(Mandatory=$true)][string]$SourcePath,
  [Parameter(Mandatory=$true)][string]$Session,
  [Parameter(Mandatory=$true)][string]$InputPath,
  [Parameter(Mandatory=$true)][string]$OutputPath,
  [int]$Frame=-1,
  [switch]$Download,
  [switch]$Jst
)
$ErrorActionPreference='Stop'
if(Test-Path -LiteralPath $OutputPath){throw 'Output already exists; use a new checkpoint path'}
$source=Get-Content -LiteralPath $SourcePath -Raw -Encoding utf8
$inputJson=Get-Content -LiteralPath $InputPath -Raw -Encoding utf8
if($Jst){$source="(async()=>{const r=JSON.parse(await ($source)(null,$inputJson));delete r.rawInput;r.queried_at=new Date().toISOString();return r;})()"}
else{$source=$source.Replace('__INPUT__',$inputJson)}
$encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($source))
$expression="eval(new TextDecoder().decode(Uint8Array.from(atob('$encoded'),c=>c.charCodeAt(0))))"
$cliArgs=@('browser',$Session,'eval',$expression)
if($Frame -ge 0){$cliArgs+=@('--frame',"$Frame")}
$raw=(& opencli @cliArgs)-join "`n"
if($LASTEXITCODE -ne 0){throw 'Page read failed; keep previous checkpoints'}
$result=$raw|ConvertFrom-Json
if($Download){
  if($result.status -ne 200 -or !$result.base64){throw 'Export did not return a file'}
  $bytes=[Convert]::FromBase64String($result.base64)
  if($bytes.Length -lt 4 -or $bytes[0] -ne 80 -or $bytes[1] -ne 75){throw 'Export is not XLSX/ZIP'}
  [IO.File]::WriteAllBytes($OutputPath,$bytes)
}else{[IO.File]::WriteAllText($OutputPath,$raw,[Text.UTF8Encoding]::new($false))}
[pscustomobject]@{path=$OutputPath;sha256=(Get-FileHash -LiteralPath $OutputPath).Hash;saved=$true}|ConvertTo-Json
