# Remote frame contract

`remote_frame_contract` is a versioned deployment field for remote RDP and
remote-display settling. It binds exact frame dimensions and reviewed volatile
rectangles. The runtime rejects a geometry change or an overlap with a declared
protected region. It retains raw frame bytes and raw lease hashes. It masks only
transient derived inputs used for pointer-settle and final pre-input content
comparisons.

The current deployment schema can declare protected regions. Desktop editing
of these reviewed regions is a separate follow-up; no runtime learning occurs.
