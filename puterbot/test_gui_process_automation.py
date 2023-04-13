import pytest

@pytest.mark.parametrize(
    "prev_objects, prev_text, new_objects, new_text, expected",
    [
        # No change in objects or text, should return an empty InputEvent
        ({}, "", {}, "", InputEvent()),
        
        # Only new text, should return a type event with the new text
        ({}, "", {}, "hello world", InputEvent(type="hello world")),
        
        # Only new objects, should return a move event with the new objects
        ({}, "", {"obj1": (100, 100)}, "", InputEvent(move=[{"obj1": (100, 100)}])),
        
        # Both new objects and text, should return a move event with the new objects and a type event with the new text
        ({}, "", {"obj1": (100, 100)}, "hello world", InputEvent(move=[{"obj1": (100, 100)}], type="hello world")),
        
        # Moving an existing object and adding new text, should return a move event with the updated object and a type event with the new text
        ({"obj1": (50, 50)}, "", {"obj1": (100, 100)}, "hello world", InputEvent(move=[{"obj1": (100, 100)}], type="hello world")),
    ]
)
def test_generate_input_event(prev_objects, prev_text, new_objects, new_text, expected):
    assert generate_prompt( new_objects,prev_objects,new_text, prev_text) == expected

