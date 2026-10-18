"""
Synthetic aapt AndroidManifest.xml tree dumps, with or without the Google Play services version meta-data.
"""

from __future__ import annotations

PLAY_SERVICES_METADATA = """      E: meta-data (line=40)
        A: android:name(0x01010003)="com.google.android.gms.version" (Raw: "com.google.android.gms.version")
        A: android:value(0x01010024)=@0x7f0b0006
"""


def manifest_tree(*, play_services: bool, package: str = "com.example.one") -> str:
    return (
        "N: android=http://schemas.android.com/apk/res/android\n"
        "  E: manifest (line=2)\n"
        f'    A: package="{package}" (Raw: "{package}")\n'
        "    E: meta-data (line=8)\n"
        '      A: android:name(0x01010003)="com.example.unrelated" (Raw: "com.example.unrelated")\n'
        "    E: application (line=12)\n"
        '      A: android:name(0x01010003)="com.example.App" (Raw: "com.example.App")\n'
        + (PLAY_SERVICES_METADATA if play_services else "")
        + "      E: activity (line=50)\n"
        '        A: android:name(0x01010003)="com.example.MainActivity" (Raw: "com.example.MainActivity")\n'
    )
