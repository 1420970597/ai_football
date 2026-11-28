"""Shared network-only APK permission policy for all release channels."""
import re

NETWORK_PERMISSIONS = frozenset({
    'android.permission.INTERNET', 'android.permission.ACCESS_NETWORK_STATE',
})


def verify_network_permissions(output: str) -> None:
    """Validate every aapt package block, including duplicate declarations."""
    packages = []
    current = None
    for line in output.splitlines():
        if line.startswith('package: '):
            current = []
            packages.append(current)
        elif line.startswith('uses-permission'):
            match = re.fullmatch(r"uses-permission(?:-sdk-[0-9]+)?: name='([^']+)'(?: .*)?", line)
            if current is None or not match:
                raise ValueError('APK permission metadata is malformed')
            current.append(match.group(1))
    if not packages:
        raise ValueError('APK package metadata is missing')
    for permissions in packages:
        if len(permissions) != len(set(permissions)) or set(permissions) != NETWORK_PERMISSIONS:
            raise ValueError('APK permissions must match the reviewed network-only allowlist without duplicates')
