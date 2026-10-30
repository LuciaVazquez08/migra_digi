# Muestra la cadena de certificados que este equipo recibe al conectarse a un servidor HTTPS.
# Solo LEE el certificado (no envía ni descarga ningún documento). Sirve para saber quién lo emite:
# la red de la empresa (inspección SSL) o el propio servidor.
#
# Uso:  powershell -ExecutionPolicy Bypass -File herramientas\ver_certificado.ps1 [-Servidor cdnva.s1gateway.com] [-GuardarRaiz]
param(
    [string]$Servidor = "cdnva.s1gateway.com",
    [switch]$GuardarRaiz
)

$tcp = New-Object Net.Sockets.TcpClient($Servidor, 443)
# Se acepta el certificado SOLO para poder leerlo; no se usa esta conexión para nada más.
$ssl = New-Object Net.Security.SslStream($tcp.GetStream(), $false, ({ $true }))
$ssl.AuthenticateAsClient($Servidor)
$cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)
$ssl.Close(); $tcp.Close()

$cadena = New-Object Security.Cryptography.X509Certificates.X509Chain
# Sin consultar revocación: si el servidor de revocación no responde no es un problema de confianza.
$cadena.ChainPolicy.RevocationMode = 'NoCheck'
$valido = $cadena.Build($cert)
"Servidor:          $Servidor"
"Válido en Windows: $valido"
foreach ($s in $cadena.ChainStatus) { "Estado de cadena:  $($s.Status) - $($s.StatusInformation.Trim())" }
"Elementos en la cadena: $($cadena.ChainElements.Count) (1 = Windows no encontró el certificado raíz del emisor)"
$i = 0
foreach ($e in $cadena.ChainElements) {
    ""
    "[$i] Emitido para: $($e.Certificate.Subject)"
    "    Emitido por:  $($e.Certificate.Issuer)"
    "    Vence:        $($e.Certificate.NotAfter)"
    "    Huella SHA1:  $($e.Certificate.Thumbprint)"
    foreach ($s in $e.ChainElementStatus) { "    Problema:     $($s.StatusInformation.Trim())" }
    $i++
}
""
"Certificados 'Fortinet' instalados en este equipo:"
Get-ChildItem Cert:\CurrentUser\Root, Cert:\LocalMachine\Root, Cert:\CurrentUser\CA, Cert:\LocalMachine\CA |
    Where-Object { $_.Subject -like '*Fortinet*' -or $_.Subject -like '*FG3K5*' } |
    ForEach-Object { "    $($_.PSParentPath.Split(':')[-1]) | $($_.Subject) | $($_.Thumbprint)" }

if ($GuardarRaiz) {
    $raiz = $cadena.ChainElements[$cadena.ChainElements.Count - 1].Certificate
    $pem = "-----BEGIN CERTIFICATE-----`r`n" + [Convert]::ToBase64String($raiz.RawData, 'InsertLineBreaks') + "`r`n-----END CERTIFICATE-----`r`n"
    $archivo = Join-Path (Get-Location) "raiz_$Servidor.pem"
    Set-Content -Path $archivo -Value $pem -Encoding ascii
    ""
    "Raíz guardada en $archivo"
    "IMPORTANTE: confirmar con sistemas que la huella SHA1 de la raíz es la del certificado oficial de la"
    "empresa ANTES de configurarlo en 'certificado_adicional'."
}
