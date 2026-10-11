"""
Verification script for SPICE environment and kernels.
"""
import os
import spiceypy as spice
import numpy as np
import pandas as pd
import openpyxl

print("=== DEPENDENCY CHECK ===")
print(f"spiceypy: {spice.__version__} (CSPICE toolkit: {spice.tkvrsn('TOOLKIT')})")
print(f"numpy:    {np.__version__}")
print(f"pandas:   {pd.__version__}")
print(f"openpyxl: {openpyxl.__version__}")

print("\n=== KERNEL FILE VERIFICATION ===")
kernel_files = ["kernels/naif0012.tls", "kernels/pck00010.tpc", "kernels/de440s.bsp"]
for k in kernel_files:
    exists = os.path.exists(k)
    size = os.path.getsize(k) if exists else 0
    print(f"{k}: exists={exists}, size={size:,} bytes")

spice.kclear()
for k in kernel_files:
    spice.furnsh(k)

count = spice.ktotal("ALL")
print(f"Furnished kernels count: {count}")

et = spice.str2et("2026-OCT-11 15:00:00 UTC")
state, lt = spice.spkezr("4", et, "J2000", "LT+S", "399")
print("\n=== SAMPLE EPHEMERIS TEST ===")
print("Target: Mars Barycenter (NAIF ID: 4) relative to Earth (NAIF ID: 399)")
print("Epoch: 2026-OCT-11 15:00:00 UTC")
print(f"Position (km):   X={state[0]:,.2f}, Y={state[1]:,.2f}, Z={state[2]:,.2f}")
print(f"Velocity (km/s): Vx={state[3]:,.4f}, Vy={state[4]:,.4f}, Vz={state[5]:,.4f}")
print(f"One-way Light Time: {lt:,.3f} seconds ({lt/60:,.2f} minutes)")

spice.kclear()
print("\nSPICE kernel verification completed successfully.")
