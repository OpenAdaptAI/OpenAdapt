from openadapt.window._windows import get_active_window,get_descendants_info
def test_check_duplicates():
    active_window = get_active_window()
    result = get_descendants_info(active_window)
    unique_props = []
    for item in result:
        if isinstance(item, dict):
            if item in unique_props:
                import ipdb; ipdb.set_trace()
                return False  # Found a duplicate, return False
            unique_props.append(item)
        else:
            return False  # Invalid item, not a dictionary
    return True  # No duplicates