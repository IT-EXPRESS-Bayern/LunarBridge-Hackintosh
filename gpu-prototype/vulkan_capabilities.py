# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Read Vulkan adapter and queue capabilities on Windows; submit no GPU work."""
import ctypes as c
import json
from pathlib import Path

vk = c.WinDLL(r'C:\Windows\System32\vulkan-1.dll')
class AppInfo(c.Structure):
    _fields_ = [('sType', c.c_uint32), ('pNext', c.c_void_p), ('pApplicationName', c.c_char_p),
                ('applicationVersion', c.c_uint32), ('pEngineName', c.c_char_p),
                ('engineVersion', c.c_uint32), ('apiVersion', c.c_uint32)]
class InstanceInfo(c.Structure):
    _fields_ = [('sType', c.c_uint32), ('pNext', c.c_void_p), ('flags', c.c_uint32),
                ('pApplicationInfo', c.POINTER(AppInfo)), ('enabledLayerCount', c.c_uint32),
                ('ppEnabledLayerNames', c.c_void_p), ('enabledExtensionCount', c.c_uint32),
                ('ppEnabledExtensionNames', c.c_void_p)]
class QueueInfo(c.Structure):
    _fields_ = [('queueFlags', c.c_uint32), ('queueCount', c.c_uint32),
                ('timestampValidBits', c.c_uint32), ('minImageTransferGranularity', c.c_uint32*3)]
vk.vkCreateInstance.argtypes = [c.POINTER(InstanceInfo), c.c_void_p, c.POINTER(c.c_void_p)]
vk.vkCreateInstance.restype = c.c_int32
vk.vkDestroyInstance.argtypes = [c.c_void_p, c.c_void_p]
vk.vkDestroyInstance.restype = None
vk.vkEnumeratePhysicalDevices.argtypes = [c.c_void_p, c.POINTER(c.c_uint32), c.c_void_p]
vk.vkEnumeratePhysicalDevices.restype = c.c_int32
vk.vkGetPhysicalDeviceProperties.argtypes = [c.c_void_p, c.c_void_p]
vk.vkGetPhysicalDeviceProperties.restype = None
vk.vkGetPhysicalDeviceQueueFamilyProperties.argtypes = [c.c_void_p, c.POINTER(c.c_uint32), c.c_void_p]
vk.vkGetPhysicalDeviceQueueFamilyProperties.restype = None
def check(result):
    if result:
        raise RuntimeError('Vulkan call failed: ' + str(result))
app = AppInfo(0, None, b'LunarLake capability inspection', 1, None, 0, 1 << 22)
info = InstanceInfo(1, None, 0, c.pointer(app), 0, None, 0, None)
instance = c.c_void_p()
check(vk.vkCreateInstance(c.byref(info), None, c.byref(instance)))
records = []
try:
    count = c.c_uint32()
    check(vk.vkEnumeratePhysicalDevices(instance, c.byref(count), None))
    if not 0 < count.value <= 64:
        raise RuntimeError('Unexpected adapter count')
    handles = (c.c_void_p * count.value)()
    check(vk.vkEnumeratePhysicalDevices(instance, c.byref(count), handles))
    for device in handles[:count.value]:
        # VkPhysicalDeviceProperties has a stable prefix. The oversized, naturally
        # aligned buffer receives the full structure without guessing its limits ABI.
        properties = (c.c_uint64 * 512)()
        vk.vkGetPhysicalDeviceProperties(device, properties)
        data = bytes(properties)
        import struct
        api, driver, vendor, product, kind = struct.unpack_from('<IIIII', data)
        name = data[20:276].split(b'\0', 1)[0].decode('utf-8', errors='replace')
        queue_count = c.c_uint32()
        vk.vkGetPhysicalDeviceQueueFamilyProperties(device, c.byref(queue_count), None)
        if queue_count.value > 64:
            raise RuntimeError('Unexpected queue family count')
        queues = (QueueInfo * queue_count.value)()
        vk.vkGetPhysicalDeviceQueueFamilyProperties(device, c.byref(queue_count), queues)
        records.append({'name': name, 'vendor_id': hex(vendor), 'device_id': hex(product),
                        'device_type': kind, 'api_version': f'{api >> 22}.{(api >> 12) & 1023}.{api & 4095}',
                        'driver_version_raw': driver, 'queue_families': [
                            {'index': i, 'count': q.queueCount, 'flags': hex(q.queueFlags),
                             'graphics': bool(q.queueFlags & 1), 'compute': bool(q.queueFlags & 2),
                             'transfer': bool(q.queueFlags & 4)} for i, q in enumerate(queues)]})
finally:
    vk.vkDestroyInstance(instance, None)
report = {'platform': 'Windows with existing Intel driver', 'adapters': records,
          'gpu_work_submitted': False, 'physical_gpu_topology_read': False,
          'macos_graphics_verified': False,
          'limitation': 'Queue families are logical Vulkan capabilities, not counts of physical Xe cores or proof of macOS support.'}
(Path(__file__).resolve().parent / 'windows-vulkan-capabilities.json').write_text(
    json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report, indent=2))
