# Component reference

Every component in `openadapt_viewer.components` is a plain function that
returns a string of HTML. Nothing renders on its own and nothing holds state,
so you can concatenate the output, drop it into a Jinja template you already
have, or pass it to `PageBuilder.add_section`.

Interactive components emit Alpine.js directives. They need `PageBuilder(...,
include_alpine=True)`, which is the default, and they need the browser to reach
`cdn.jsdelivr.net` when the page opens.

To regenerate this list after changing a signature:

```python
import inspect
import openadapt_viewer.components as c

for name in c.__all__:
    fn = getattr(c, name)
    print(f"{name}{inspect.signature(fn)}")
```

### `screenshot_display`

Render a screenshot with optional overlays.

```python
screenshot_display(image_path: 'str | Path | None' = None, width: 'int' = 800, height: 'int' = 450, overlays: 'list[Overlay] | None' = None, caption: 'str | None' = None, embed_image: 'bool' = False, placeholder_text: 'str' = 'No screenshot available', class_name: 'str' = '') -> 'str'
```

### `playback_controls`

Render playback controls for step navigation.

```python
playback_controls(step_count: 'int' = 1, initial_step: 'int' = 0, speeds: 'list[float] | None' = None, default_speed: 'float' = 1.0, show_step_counter: 'bool' = True, alpine_data_name: 'str' = 'playback', class_name: 'str' = '') -> 'str'
```

### `timeline`

Render a timeline progress bar.

```python
timeline(step_count: 'int' = 1, current_step: 'int' = 0, step_labels: 'list[str] | None' = None, clickable: 'bool' = True, show_markers: 'bool' = False, alpine_data_name: 'str' = 'playback', class_name: 'str' = '') -> 'str'
```

### `action_display`

Render an action display with badge and details.

```python
action_display(action_type: 'str | None' = None, action_details: 'dict[str, Any] | None' = None, show_badge: 'bool' = True, show_details: 'bool' = True, show_reasoning: 'bool' = False, reasoning: 'str | None' = None, class_name: 'str' = '') -> 'str'
```

### `metrics_card`

Render a single metrics card.

```python
metrics_card(label: 'str', value: 'str | float', change: 'float | None' = None, color: 'str' = 'default', icon: 'str | None' = None, class_name: 'str' = '') -> 'str'
```

### `metrics_grid`

Render a grid of metrics cards.

```python
metrics_grid(cards: 'list[dict[str, Any]]', columns: 'int' = 4, class_name: 'str' = '') -> 'str'
```

### `filter_bar`

Render a filter bar with multiple dropdowns and optional search.

```python
filter_bar(filters: 'list[FilterConfig]', search_placeholder: 'str | None' = None, search_model: 'str | None' = None, alpine_data_name: 'str' = 'filters', class_name: 'str' = '') -> 'str'
```

### `filter_dropdown`

Render a single filter dropdown.

```python
filter_dropdown(filter_id: 'str', label: 'str', options: 'list[FilterOption] | list[str]', default_value: 'str' = '', alpine_model: 'str | None' = None, class_name: 'str' = '') -> 'str'
```

### `selectable_list`

Render a list with selection support.

```python
selectable_list(items: 'list[ListItemConfig]', title: 'str | None' = None, subtitle: 'str | None' = None, max_height: 'str' = '600px', alpine_data_name: 'str' = 'list', selected_item_var: 'str' = 'selectedItem', on_select: 'str | None' = None, class_name: 'str' = '') -> 'str'
```

### `list_item`

Render a single list item.

```python
list_item(item_id: 'str', title: 'str', subtitle: 'str | None' = None, badge: 'str | None' = None, badge_color: 'str' = 'info', selected: 'bool' = False, click_handler: 'str | None' = None, class_name: 'str' = '') -> 'str'
```

### `badge`

Render a status badge.

```python
badge(text: 'str', color: 'str' = 'info', size: 'str' = 'md', class_name: 'str' = '') -> 'str'
```

### `video_playback`

Render a video playback component from screenshot frames.

```python
video_playback(frames: 'list[ScreenshotFrame] | None' = None, width: 'int' = 960, height: 'int' = 540, autoplay: 'bool' = False, loop: 'bool' = False, show_controls: 'bool' = True, show_timeline: 'bool' = True, show_frame_counter: 'bool' = True, default_fps: 'float' = 2.0, speeds: 'list[float] | None' = None, embed_images: 'bool' = False, alpine_data_name: 'str' = 'videoPlayer', class_name: 'str' = '') -> 'str'
```

### `video_playback_with_actions`

Video playback with integrated action details panel.

```python
video_playback_with_actions(frames: 'list[ScreenshotFrame] | None' = None, width: 'int' = 960, height: 'int' = 540, show_action_details: 'bool' = True, **kwargs) -> 'str'
```

### `action_timeline`

Render an action timeline with seek functionality.

```python
action_timeline(actions: 'list[TimelineAction] | None' = None, duration: 'float | None' = None, current_time: 'float' = 0, width: 'str' = '100%', height: 'int' = 60, show_labels: 'bool' = True, show_time_markers: 'bool' = True, clickable: 'bool' = True, alpine_sync_var: 'str | None' = None, on_seek: 'str | None' = None, class_name: 'str' = '') -> 'str'
```

### `action_timeline_vertical`

Render a vertical action list/timeline.

```python
action_timeline_vertical(actions: 'list[TimelineAction] | None' = None, height: 'str' = '400px', show_details: 'bool' = True, clickable: 'bool' = True, alpine_sync_var: 'str | None' = None, class_name: 'str' = '') -> 'str'
```

### `comparison_view`

Render a side-by-side comparison view.

```python
comparison_view(left_data: 'ComparisonData | None' = None, right_data: 'ComparisonData | None' = None, width: 'int' = 1200, height: 'int' = 450, show_diff: 'bool' = True, sync_playback: 'bool' = True, show_actions: 'bool' = True, click_tolerance: 'float' = 0.05, class_name: 'str' = '') -> 'str'
```

### `overlay_comparison`

Render a single screenshot with overlays for both human and predicted actions.

```python
overlay_comparison(base_screenshot: 'str | None' = None, human_click: 'dict | None' = None, predicted_click: 'dict | None' = None, width: 'int' = 800, height: 'int' = 450, show_distance: 'bool' = True, class_name: 'str' = '') -> 'str'
```

### `action_type_filter`

Render an action type filter component.

```python
action_type_filter(action_types: 'list[ActionTypeConfig] | None' = None, selected_types: 'list[str] | None' = None, show_counts: 'bool' = True, show_all_option: 'bool' = True, multi_select: 'bool' = True, alpine_model: 'str | None' = None, on_change: 'str | None' = None, layout: 'str' = 'horizontal', class_name: 'str' = '') -> 'str'
```

### `action_type_pills`

Render a compact pill-style action type filter.

```python
action_type_pills(action_types: 'list[ActionTypeConfig] | None' = None, selected_types: 'list[str] | None' = None, alpine_model: 'str | None' = None, on_change: 'str | None' = None, class_name: 'str' = '') -> 'str'
```

### `action_type_dropdown`

Render a dropdown-style action type filter with checkboxes.

```python
action_type_dropdown(action_types: 'list[ActionTypeConfig] | None' = None, selected_types: 'list[str] | None' = None, placeholder: 'str' = 'Filter by action type', alpine_model: 'str | None' = None, on_change: 'str | None' = None, class_name: 'str' = '') -> 'str'
```

### `failure_analysis_panel`

Render a comprehensive failure analysis panel.

```python
failure_analysis_panel(failures: 'list[FailureRecord] | None' = None, total_tasks: 'int' = 0, show_categories: 'bool' = True, show_list: 'bool' = True, show_details: 'bool' = True, on_select_failure: 'str | None' = None, class_name: 'str' = '') -> 'str'
```

### `failure_summary_card`

Render a compact failure summary card.

```python
failure_summary_card(total_failures: 'int' = 0, total_tasks: 'int' = 0, top_error_type: 'str | None' = None, top_error_count: 'int' = 0, class_name: 'str' = '') -> 'str'
```

