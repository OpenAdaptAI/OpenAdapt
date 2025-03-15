"""Process graph-based replay strategy using OmniParser and Gemini 2.0.

This strategy:
1. Uses OmniParser for parsing visual state and Gemini 2.0 for state evaluation
2. Takes natural language task descriptions instead of recording IDs
3. Processes coalesced actions from events.py 
4. Builds and maintains a process graph G=(V,E) where:
   - V represents States
   - E represents Actions
   - Graph is constructed before replay based on recording + task description
   - Graph is updated during replay based on observed states
"""

import json
import math
import time
import uuid
from typing import List, Optional, Dict, Union, Literal, Any
import numpy as np

from pydantic import BaseModel, Field
from PIL import Image
from json_repair import repair_json, loads as repair_loads

from openadapt import adapters, common, models, utils, vision
from openadapt.custom_logger import logger
from openadapt.db import crud
from openadapt.strategies.base import BaseReplayStrategy
from openadapter.providers.omniparser import OmniParserProvider


# Pydantic models for structured data
class RecognitionCriterion(BaseModel):
    """Criteria for recognizing a state"""
    type: Literal["window_title", "ui_element_present", "visual_template"]
    pattern: Optional[str] = None
    threshold: Optional[float] = None
    element_descriptor: Optional[str] = None


class ActionParameter(BaseModel):
    """Parameters for an action"""
    target_element: Optional[str] = None
    text_input: Optional[str] = None
    click_type: Optional[Literal["single", "double", "right"]] = None
    coordinate_type: Optional[Literal["absolute", "relative"]] = None


class ActionModel(BaseModel):
    """Model for an action in the process"""
    name: str
    description: str
    parameters: ActionParameter


class Condition(BaseModel):
    """Condition for a transition"""
    type: Literal["element_state", "data_value", "previous_action"]
    description: str


class Transition(BaseModel):
    """Transition between states"""
    from_state: str = Field(..., alias="from")
    to_state: str = Field(..., alias="to")
    action: ActionModel
    condition: Optional[Condition] = None


class Branch(BaseModel):
    """Branch in a decision point"""
    condition: str
    next_state: str


class DecisionPoint(BaseModel):
    """Decision point in the process"""
    state: str
    description: str
    branches: List[Branch]


class Loop(BaseModel):
    """Loop in the process"""
    start_state: str
    end_state: str
    exit_condition: str
    description: str


class StateModel(BaseModel):
    """Model for a state in the process"""
    name: str
    description: str
    recognition_criteria: List[RecognitionCriterion]


class ProcessAnalysis(BaseModel):
    """Complete model of a process"""
    process_name: str
    description: str
    states: List[StateModel]
    transitions: List[Transition]
    loops: List[Loop]
    decision_points: List[DecisionPoint]


class StateTrajectoryEntry(BaseModel):
    """Entry in the state trajectory"""
    state_name: Optional[str] = None
    action_name: Optional[str] = None
    timestamp: float


class CurrentStateMatch(BaseModel):
    """Result of matching current state to graph"""
    matched_state_name: str
    confidence: float
    reasoning: str


class UIElement(BaseModel):
    """UI element in the visual state"""
    type: str
    text: Optional[str] = None
    bounds: Dict[str, int]
    description: str
    is_interactive: bool


class VisualState(BaseModel):
    """Visual state representation"""
    window_title: str
    ui_elements: List[UIElement]
    screenshot_timestamp: float


class AbstractState:
    """Represents an abstract state in the process graph with recognition logic."""
    
    def __init__(self, name, description, recognition_criteria):
        self.id = str(uuid.uuid4())
        self.name = name
        self.description = description
        self.recognition_criteria = recognition_criteria
        self.example_screenshots = []
        
    def match_rules(self, current_state, trajectory=None):
        """Apply rule-based matching using recognition criteria."""
        for criterion in self.recognition_criteria:
            if not self._evaluate_criterion(criterion, current_state):
                return False
        return True
    
    def _evaluate_criterion(self, criterion, state):
        """Evaluate a single recognition criterion against current state."""
        criterion_type = criterion["type"]
        
        if criterion_type == "window_title":
            if not state.window_event or not state.window_event.title:
                return False
            return criterion["pattern"] in state.window_event.title
            
        elif criterion_type == "ui_element_present":
            if not state.visual_data:
                return False
            return any(
                criterion["element_descriptor"] in element["description"]
                for element in state.visual_data
            )
            
        elif criterion_type == "visual_template":
            # Match against example screenshots
            if not self.example_screenshots:
                return False
            return any(
                vision.get_image_similarity(state.screenshot.image, example)[0] > criterion.get("threshold", 0.8)
                for example in self.example_screenshots
            )
            
        return False
        
    def add_example(self, screenshot):
        """Add example screenshot for visual matching."""
        self.example_screenshots.append(screenshot)
        
    def to_dict(self):
        """Convert to dictionary representation."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "recognition_criteria": self.recognition_criteria
        }


class AbstractAction:
    """Represents an abstract action with parameters to be instantiated."""
    
    def __init__(self, name, description, parameters):
        self.id = str(uuid.uuid4())
        self.name = name
        self.description = description
        self.parameters = parameters
        
    def to_dict(self):
        """Convert to dictionary representation."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


class ProcessGraph:
    """Enhanced process graph with abstract states and conditional transitions."""
    
    def __init__(self):
        self.nodes = set()
        self.edges = []
        self.conditions = {}  # Maps (from_state, action, to_state) to condition logic
        self.description = ""
        
    def add_node(self, node):
        """Add a node to the graph."""
        self.nodes.add(node)
        
    def add_edge(self, from_state, action, to_state):
        """Add an edge to the graph."""
        self.add_node(from_state)
        self.add_node(action)
        self.add_node(to_state)
        self.edges.append((from_state, action, to_state))
        
    def add_condition(self, from_state, action, to_state, condition):
        """Add a condition to an edge."""
        key = (from_state.id, action.id, to_state.id)
        self.conditions[key] = condition
        
    def get_abstract_states(self):
        """Get all abstract states in the graph."""
        return [node for node in self.nodes if isinstance(node, AbstractState)]
        
    def get_state_by_name(self, name):
        """Find a state by name."""
        for node in self.nodes:
            if isinstance(node, AbstractState) and node.name == name:
                return node
        return None
        
    def get_possible_actions(self, state):
        """Get possible actions from a state, considering conditions."""
        possible_actions = []
        
        for from_state, action, to_state in self.edges:
            if from_state.id == state.id:
                key = (from_state.id, action.id, to_state.id)
                if key in self.conditions:
                    # For now, we include conditional actions
                    # In a full implementation, would need to evaluate conditions
                    possible_actions.append((action, to_state))
                else:
                    possible_actions.append((action, to_state))
                
        return possible_actions
        
    def set_description(self, description):
        """Set the overall description of the process."""
        self.description = description
        
    def get_description(self):
        """Get the overall description of the process."""
        return self.description
        
    def to_model(self) -> ProcessAnalysis:
        """Convert graph to Pydantic model for serialization."""
        states = [
            StateModel(
                name=state.name,
                description=state.description,
                recognition_criteria=[
                    RecognitionCriterion(**criterion) 
                    for criterion in state.recognition_criteria
                ]
            )
            for state in self.get_abstract_states()
        ]
        
        transitions = []
        for from_state, action, to_state in self.edges:
            if isinstance(from_state, AbstractState) and isinstance(to_state, AbstractState):
                key = (from_state.id, action.id, to_state.id)
                transition = Transition(
                    from_state=from_state.name,
                    to_state=to_state.name,
                    action=ActionModel(
                        name=action.name,
                        description=action.description,
                        parameters=ActionParameter(**action.parameters)
                    )
                )
                if key in self.conditions:
                    transition.condition = Condition(**self.conditions[key])
                transitions.append(transition)
        
        # Build loops and decision points using a simple algorithm
        loops = self._detect_loops()
        decision_points = self._detect_decision_points()
        
        return ProcessAnalysis(
            process_name=self.description.split("\n")[0] if self.description else "Unnamed Process",
            description=self.description,
            states=states,
            transitions=transitions,
            loops=loops,
            decision_points=decision_points
        )
    
    def _detect_loops(self) -> List[Loop]:
        """Simple loop detection algorithm."""
        loops = []
        # Map state names to IDs for easier lookup
        state_id_to_name = {state.id: state.name for state in self.get_abstract_states()}
        
        # Find cycles in the graph using DFS
        visited = set()
        path = []
        
        def dfs(node_id):
            if node_id in path:
                # Found a cycle
                cycle_start = path.index(node_id)
                cycle = path[cycle_start:]
                # Only process if the cycle involves states (not just actions)
                state_ids = [node_id for node_id in cycle if node_id in state_id_to_name]
                if len(state_ids) > 1:
                    loops.append(Loop(
                        start_state=state_id_to_name[state_ids[0]],
                        end_state=state_id_to_name[state_ids[-1]],
                        exit_condition="Condition to exit loop",
                        description=f"Loop from {state_id_to_name[state_ids[0]]} to {state_id_to_name[state_ids[-1]]}"
                    ))
                return
            
            if node_id in visited:
                return
                
            visited.add(node_id)
            path.append(node_id)
            
            # Find all outgoing edges
            for from_state, _, to_state in self.edges:
                if from_state.id == node_id:
                    dfs(to_state.id)
                    
            path.pop()
        
        # Start DFS from each state
        for state in self.get_abstract_states():
            dfs(state.id)
            
        return loops
    
    def _detect_decision_points(self) -> List[DecisionPoint]:
        """Detect states with multiple outgoing transitions."""
        decision_points = []
        state_id_to_name = {state.id: state.name for state in self.get_abstract_states()}
        
        # Count outgoing edges for each state
        outgoing_counts = {}
        for from_state, _, to_state in self.edges:
            if from_state.id not in outgoing_counts:
                outgoing_counts[from_state.id] = []
            outgoing_counts[from_state.id].append(to_state.id)
        
        # States with multiple outgoing edges are decision points
        for state_id, destinations in outgoing_counts.items():
            if state_id in state_id_to_name and len(destinations) > 1:
                branches = []
                for dest_id in destinations:
                    if dest_id in state_id_to_name:
                        branches.append(Branch(
                            condition=f"Condition to go to {state_id_to_name[dest_id]}",
                            next_state=state_id_to_name[dest_id]
                        ))
                
                if branches:
                    decision_points.append(DecisionPoint(
                        state=state_id_to_name[state_id],
                        description=f"Decision point at {state_id_to_name[state_id]}",
                        branches=branches
                    ))
                    
        return decision_points
        
    def to_json(self):
        """Convert graph to JSON string."""
        return self.to_model().model_dump_json(indent=2)
        
    def update_with_observation(self, observed_state, previous_state, latest_action):
        """Update graph with observed state during execution."""
        # Find abstract states that match the observed state
        similar_state = None
        highest_similarity = 0.0
        
        for state in self.get_abstract_states():
            similarity = self._calculate_state_similarity(observed_state, state)
            if similarity > highest_similarity:
                highest_similarity = similarity
                similar_state = state
        
        # Create a new state if no good match
        if highest_similarity < 0.7:
            similar_state = self._create_new_state_from_observation(observed_state)
        
        # If we have a previous state and action, create or update a transition
        if previous_state and latest_action:
            # Check if transition already exists
            transition_exists = False
            for from_state, action, to_state in self.edges:
                if (from_state.id == previous_state.id and 
                    action.name == latest_action.name and
                    to_state.id == similar_state.id):
                    transition_exists = True
                    break
                    
            if not transition_exists:
                # Create a new abstract action from the latest action
                action = AbstractAction(
                    name=latest_action.name,
                    description=f"Action {latest_action.name}",
                    parameters=self._extract_action_parameters(latest_action)
                )
                
                # Add the edge
                self.add_edge(previous_state, action, similar_state)
                
        return similar_state
        
    def _calculate_state_similarity(self, observed_state, abstract_state):
        """Calculate similarity between observed state and abstract state."""
        # Use rule-based matching first
        if abstract_state.match_rules(observed_state):
            return 0.9  # High confidence if rules match
            
        # Fall back to visual similarity if we have example screenshots
        if abstract_state.example_screenshots and observed_state.screenshot:
            visual_similarities = [
                vision.get_image_similarity(observed_state.screenshot.image, example)[0]
                for example in abstract_state.example_screenshots
            ]
            return max(visual_similarities) if visual_similarities else 0.0
            
        return 0.0
        
    def _create_new_state_from_observation(self, observed_state):
        """Create a new abstract state from an observed state."""
        # Generate a name for the state based on window title
        name = "State_" + str(len(self.get_abstract_states()) + 1)
        if observed_state.window_event and observed_state.window_event.title:
            name = f"State_{observed_state.window_event.title[:20]}"
            
        # Create recognition criteria
        criteria = []
        if observed_state.window_event and observed_state.window_event.title:
            criteria.append({
                "type": "window_title",
                "pattern": observed_state.window_event.title
            })
            
        if observed_state.visual_data:
            # Add criteria based on visible UI elements
            for element in observed_state.visual_data[:3]:  # Limit to a few key elements
                if element.get("description"):
                    criteria.append({
                        "type": "ui_element_present",
                        "element_descriptor": element["description"]
                    })
                    
        state = AbstractState(
            name=name,
            description=f"State with window title: {observed_state.window_event.title if observed_state.window_event else 'Unknown'}",
            recognition_criteria=criteria
        )
        
        # Add screenshot as example for visual matching
        if observed_state.screenshot:
            state.add_example(observed_state.screenshot.image)
            
        self.add_node(state)
        return state
        
    def _extract_action_parameters(self, action_event):
        """Extract parameters from an action event."""
        parameters = {}
        
        if action_event.name in common.MOUSE_EVENTS:
            parameters["target_element"] = action_event.active_segment_description
            if "click" in action_event.name:
                if "double" in action_event.name:
                    parameters["click_type"] = "double"
                elif "right" in action_event.name:
                    parameters["click_type"] = "right"
                else:
                    parameters["click_type"] = "single"
                    
        elif action_event.name in common.KEYBOARD_EVENTS:
            if action_event.text:
                parameters["text_input"] = action_event.text
                
        return parameters


class State:
    """Represents a concrete state during execution."""
    
    def __init__(self, screenshot, window_event, browser_event=None, visual_data=None):
        self.id = str(uuid.uuid4())
        self.screenshot = screenshot
        self.window_event = window_event
        self.browser_event = browser_event
        self.visual_data = visual_data or []


class ProcessGraphStrategy(BaseReplayStrategy):
    """Strategy using process graphs, OmniParser and Gemini 2.0 Flash."""
    
    def __init__(
        self,
        task_description: str,
        recording_id: int = None,
    ) -> None:
        """Initialize with task description rather than recording ID."""
        # Find best matching recording if not provided
        if not recording_id:
            recording_id = self._find_matching_recording(task_description)
        
        db_session = crud.get_new_session()
        self.recording = crud.get_recording(db_session, recording_id)
        super().__init__(self.recording)
        
        self.task_description = task_description
        
        # Initialize OmniParser service
        self.omniparser_provider = OmniParserProvider()
        self._ensure_omniparser_running()
        
        # Initialize tracking
        self.state_action_history = []  # List of (state, action) pairs
        self.action_history = []
        self.current_state = None
        self.current_abstract_state = None
        
        # Build graph before replay
        self.process_graph = self._build_generalizable_process_graph(task_description)
    
    def _ensure_omniparser_running(self):
        """Ensure OmniParser is running, deploying if necessary."""
        status = self.omniparser_provider.status()
        if not status['services']:
            logger.info("Deploying OmniParser...")
            self.omniparser_provider.deploy()
            self.omniparser_provider.stack.create_service()
    
    def _find_matching_recording(self, task_description: str) -> int:
        """Find recording with most similar task description using vector similarity."""
        db_session = crud.get_new_session()
        recordings = crud.get_all_recordings(db_session)
        best_match = None
        highest_similarity = -1
        
        for recording in recordings:
            if not recording.task_description:
                continue
                
            similarity = self._calculate_text_similarity(task_description, recording.task_description)
            if similarity > highest_similarity:
                highest_similarity = similarity
                best_match = recording.id
                
        if best_match is None:
            # If no good match, use the most recent recording
            recordings_sorted = sorted(recordings, key=lambda r: r.timestamp, reverse=True)
            if recordings_sorted:
                best_match = recordings_sorted[0].id
            else:
                raise ValueError("No recordings found in the database.")
                
        return best_match
    
    def _calculate_text_similarity(self, text1, text2):
        """Calculate similarity between two text strings."""
        # Simple word overlap similarity
        if not text1 or not text2:
            return 0.0
            
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        
        if not words1 or not words2:
            return 0.0
            
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        
        return len(intersection) / len(union)
    
    def _build_generalizable_process_graph(self, task_description):
        """Build a generalizable process graph using multi-phase approach with MMMs."""
        # Get coalesced actions
        processed_actions = self.recording.processed_action_events
        
        # Phase 1: Process Understanding - Analyze the entire workflow
        process_model = self._analyze_entire_process(processed_actions, task_description)
        
        # Phase 2: Graph Construction - Build abstract graph from understanding
        initial_graph = self._construct_abstract_graph(process_model)
        
        # Phase 3: Graph Validation - Test and refine by walking through recording
        refined_graph = self._validate_and_refine_graph(initial_graph, processed_actions)
        
        return refined_graph
        
    def _select_representative_screenshots(self, action_events, max_images=10):
        """Select representative screenshots from the action events."""
        if not action_events:
            return []
            
        # If few actions, use all screenshots
        if len(action_events) <= max_images:
            return [action.screenshot.image for action in action_events if action.screenshot]
            
        # Otherwise, select evenly spaced screenshots
        step = len(action_events) // max_images
        selected_actions = action_events[::step]
        
        # Add the last action if not included
        if action_events[-1] not in selected_actions:
            selected_actions.append(action_events[-1])
            
        return [action.screenshot.image for action in selected_actions if action.screenshot]
        
    def _analyze_entire_process(self, actions, task_description):
        """Have Gemini analyze the entire recording to understand the process structure."""
        key_screenshots = self._select_representative_screenshots(actions)
        
        # Generate schema JSON for the prompt
        schema_json = ProcessAnalysis.model_json_schema()
        
        system_prompt = "You are an expert in understanding user interface workflows."
        prompt = f"""
        Analyze this UI automation sequence and identify:
        1. The high-level steps in the process
        2. Any repetitive patterns or loops
        3. Decision points where the workflow might branch
        4. The semantic meaning of each major state
        
        Task description: {task_description}
        
        RESPOND USING THE FOLLOWING JSON SCHEMA:
        ```json
        {json.dumps(schema_json, indent=2)}
        ```
        
        Your response must strictly follow this schema and be valid JSON.
        """
        
        process_analysis_text = self.prompt_gemini(prompt, system_prompt, key_screenshots)
        
        # Use json_repair for robust parsing
        try:
            # Direct parsing if possible
            process_data = repair_loads(process_analysis_text)
            process_model = ProcessAnalysis(**process_data)
            return process_model
        except Exception as e:
            logger.warning(f"Initial JSON parsing failed: {e}")
            
            # Try to repair potentially broken JSON
            try:
                repaired_json = repair_json(process_analysis_text, ensure_ascii=False)
                process_data = json.loads(repaired_json)
                process_model = ProcessAnalysis(**process_data)
                return process_model
            except Exception as repair_e:
                logger.error(f"JSON repair also failed: {repair_e}")
                
                # Last resort: try direct object return
                try:
                    process_data = repair_json(process_analysis_text, return_objects=True)
                    process_model = ProcessAnalysis(**process_data)
                    return process_model
                except Exception as final_e:
                    logger.error(f"All JSON parsing methods failed: {final_e}")
                    return self._fallback_process_analysis(actions, task_description)
    
    def _fallback_process_analysis(self, actions, task_description):
        """Create a simple process model if all parsing fails."""
        logger.warning("Using fallback process analysis")
        
        # Create a simple linear process model
        states = []
        transitions = []
        
        # Create a state for each key action
        key_actions = actions[::max(1, len(actions) // 5)]  # At most 5 states
        
        for i, action in enumerate(key_actions):
            state_name = f"State_{i+1}"
            state_description = f"State after {action.name} action"
            
            # Create recognition criteria
            criteria = []
            if action.window_event and action.window_event.title:
                criteria.append({
                    "type": "window_title",
                    "pattern": action.window_event.title
                })
                
            states.append(StateModel(
                name=state_name,
                description=state_description,
                recognition_criteria=criteria
            ))
            
            # Create transition to next state
            if i < len(key_actions) - 1:
                next_action = key_actions[i+1]
                transitions.append(Transition(
                    from_state=state_name,
                    to_state=f"State_{i+2}",
                    action=ActionModel(
                        name=next_action.name,
                        description=f"{next_action.name} action",
                        parameters=ActionParameter()
                    )
                ))
                
        return ProcessAnalysis(
            process_name="Fallback Process",
            description=f"Fallback process for task: {task_description}",
            states=states,
            transitions=transitions,
            loops=[],
            decision_points=[]
        )
    
    def _construct_abstract_graph(self, process_model):
        """Construct an abstract process graph based on the process understanding."""
        graph = ProcessGraph()
        graph.set_description(process_model.description)
        
        # Create abstract state definitions based on process model
        for state_def in process_model.states:
            state = AbstractState(
                name=state_def.name,
                description=state_def.description,
                recognition_criteria=[criterion.model_dump() for criterion in state_def.recognition_criteria]
            )
            graph.add_node(state)
            
        # Create transitions with abstract actions
        for transition in process_model.transitions:
            from_state = graph.get_state_by_name(transition.from_state)
            to_state = graph.get_state_by_name(transition.to_state)
            
            if from_state and to_state:
                action = AbstractAction(
                    name=transition.action.name,
                    description=transition.action.description,
                    parameters=transition.action.parameters.model_dump()
                )
                graph.add_edge(from_state, action, to_state)
                
                # Add conditional branches if present
                if transition.condition:
                    graph.add_condition(from_state, action, to_state, transition.condition.model_dump())
            
        return graph
    
    def _validate_and_refine_graph(self, graph, actions):
        """Test the graph against recorded actions and refine it with Gemini's help."""
        # Simulate walking through the recording using the graph
        simulation_results = self._simulate_graph_execution(graph, actions)
        
        if simulation_results["success"]:
            return graph
            
        # If simulation failed, ask Gemini to refine the graph
        system_prompt = "You are an expert in refining process models."
        prompt = f"""
        The process graph failed to match the recording at these points:
        {simulation_results["failures"]}
        
        Current graph: {graph.to_json()}
        
        Please refine the graph to better match the recorded process while 
        maintaining generalizability. Consider:
        1. Adding missing states or transitions
        2. Adjusting state recognition criteria
        3. Modifying action parameters
        4. Adding conditional logic
        
        RESPOND USING THE SAME JSON SCHEMA AS THE CURRENT GRAPH.
        """
        
        refinements_text = self.prompt_gemini(prompt, system_prompt, simulation_results["screenshots"])
        
        try:
            refinements_data = repair_loads(refinements_text)
            refined_model = ProcessAnalysis(**refinements_data)
            refined_graph = self._construct_abstract_graph(refined_model)
            
            # Check if refinement improved the simulation
            new_failures = len(self._simulate_graph_execution(refined_graph, actions)["failures"])
            old_failures = len(simulation_results["failures"])
            
            if new_failures < old_failures:
                return refined_graph
            return graph
            
        except Exception as e:
            logger.error(f"Failed to parse graph refinements: {e}")
            return graph
    
    def _simulate_graph_execution(self, graph, actions):
        """Simulate executing the graph with the recorded actions."""
        failures = []
        screenshots = []
        current_state = None
        
        for i, action in enumerate(actions):
            # If first action, find initial state
            if i == 0:
                state = State(action.screenshot, action.window_event, action.browser_event)
                matched_state = None
                highest_similarity = 0.0
                
                for abstract_state in graph.get_abstract_states():
                    similarity = graph._calculate_state_similarity(state, abstract_state)
                    if similarity > highest_similarity:
                        highest_similarity = similarity
                        matched_state = abstract_state
                        
                if highest_similarity < 0.7:
                    failures.append(f"Failed to match initial state at action {i}")
                    screenshots.append(action.screenshot.image)
                
                current_state = matched_state
                continue
                
            # For subsequent actions, check if the graph has a transition
            if current_state:
                possible_actions = graph.get_possible_actions(current_state)
                
                # Check if any action matches the recorded action
                action_match = False
                for graph_action, next_state in possible_actions:
                    if graph_action.name == action.name:
                        action_match = True
                        current_state = next_state
                        break
                        
                if not action_match:
                    failures.append(f"No matching action '{action.name}' from state '{current_state.name}' at action {i}")
                    screenshots.append(action.screenshot.image)
        
        return {
            "success": len(failures) == 0,
            "failures": failures,
            "screenshots": screenshots[:5]  # Limit to 5 screenshots for prompt size
        }
    
    def get_next_action_event(
        self,
        screenshot: models.Screenshot,
        window_event: models.WindowEvent,
    ) -> models.ActionEvent:
        """Determine next action using the process graph and runtime adaptation."""
        # Create current state representation
        current_state = State(
            screenshot=screenshot,
            window_event=window_event
        )
        
        # Parse visual state with OmniParser
        visual_data = self._parse_state_with_omniparser(screenshot.image)
        current_state.visual_data = visual_data
        
        # Update graph with actual observed state
        previous_abstract_state = self.current_abstract_state
        latest_action = self.action_history[-1] if self.action_history else None
        
        self.current_abstract_state = self.process_graph.update_with_observation(
            current_state, 
            previous_abstract_state,
            latest_action
        )
        
        self.current_state = current_state
        
        # Find possible next actions in graph
        possible_actions = self.process_graph.get_possible_actions(self.current_abstract_state)
        
        if not possible_actions:
            # No actions available - either reached end state or unexpected state
            if len(self.action_history) > 0:
                # We've taken at least one action, so this might be the end
                raise StopIteration("No further actions available in the process graph")
            else:
                # No actions taken yet - generate one with Gemini
                next_action = self._generate_action_with_gemini()
                self.action_history.append(next_action)
                return next_action
        
        if len(possible_actions) == 1:
            # Single clear action to take
            action, next_state = possible_actions[0]
            next_action = self._instantiate_abstract_action(action, current_state)
        else:
            # Multiple possible actions - use Gemini to decide
            next_action = self._decide_between_actions(possible_actions, current_state)
        
        self.state_action_history.append((self.current_abstract_state, next_action))
        self.action_history.append(next_action)
        
        return next_action
    
    def _parse_state_with_omniparser(self, screenshot_image):
        """Use OmniParser to parse the visual state."""
        try:
            # Convert PIL Image to bytes
            import io
            img_byte_arr = io.BytesIO()
            screenshot_image.save(img_byte_arr, format='PNG')
            img_bytes = img_byte_arr.getvalue()
            
            # Call OmniParser API
            result = self.omniparser_provider.parse_screenshot(img_bytes)
            
            # Transform the result into our expected format
            ui_elements = []
            for element in result.get("elements", []):
                ui_elements.append({
                    "type": element.get("type", "unknown"),
                    "text": element.get("text", ""),
                    "bounds": element.get("bounds", {"x": 0, "y": 0, "width": 0, "height": 0}),
                    "description": element.get("description", ""),
                    "is_interactive": element.get("is_interactive", False)
                })
                
            return ui_elements
            
        except Exception as e:
            logger.error(f"Error parsing state with OmniParser: {e}")
            return []
    
    def _instantiate_abstract_action(self, abstract_action, current_state):
        """Convert abstract action to concrete ActionEvent based on current state."""
        try:
            # Use parameters from abstract action if possible
            params = abstract_action.parameters
            
            if abstract_action.name in common.MOUSE_EVENTS:
                # Create a mouse action
                action_event = models.ActionEvent(
                    name=abstract_action.name,
                    screenshot=current_state.screenshot,
                    window_event=current_state.window_event,
                    recording=self.recording
                )
                
                # If we have a target element, find its coordinates
                if params.get("target_element"):
                    target_element = None
                    for element in current_state.visual_data:
                        if params["target_element"] in element.get("description", ""):
                            target_element = element
                            break
                            
                    if target_element:
                        bounds = target_element.get("bounds", {})
                        # Calculate center of element
                        center_x = bounds.get("x", 0) + bounds.get("width", 0) / 2
                        center_y = bounds.get("y", 0) + bounds.get("height", 0) / 2
                        
                        action_event.mouse_x = center_x
                        action_event.mouse_y = center_y
                        action_event.active_segment_description = params["target_element"]
                    else:
                        # If target not found, use Gemini to identify coordinates
                        action_event = self._locate_target_with_gemini(
                            params["target_element"], 
                            abstract_action.name,
                            current_state
                        )
                else:
                    # Use Gemini to decide where to click
                    action_event = self._locate_target_with_gemini(
                        None, 
                        abstract_action.name,
                        current_state
                    )
                    
                return action_event
                
            elif abstract_action.name in common.KEYBOARD_EVENTS:
                # Create a keyboard action
                action_event = models.ActionEvent(
                    name=abstract_action.name,
                    screenshot=current_state.screenshot,
                    window_event=current_state.window_event,
                    recording=self.recording
                )
                
                if params.get("text_input"):
                    # For "type" action, convert to actual keypresses
                    action_event = models.ActionEvent.from_dict({
                        "name": "type",
                        "text": params["text_input"]
                    })
                    action_event.screenshot = current_state.screenshot
                    action_event.window_event = current_state.window_event
                    action_event.recording = self.recording
                    
                return action_event
                
            else:
                # For other actions, use Gemini
                return self._generate_action_with_gemini(abstract_action.name)
                
        except Exception as e:
            logger.error(f"Error instantiating action: {e}")
            return self._generate_action_with_gemini()
    
    def _locate_target_with_gemini(self, target_description, action_name, current_state):
        """Use Gemini to locate a target on the screen."""
        system_prompt = "You are an expert in UI automation and element identification."
        prompt = f"""
        Identify the coordinates to perform a {action_name} action.
        
        {f'The target is described as: {target_description}' if target_description else 'Find the most appropriate element to interact with based on the current state.'}
        
        Analyze the screenshot and provide the x,y coordinates where the action should be performed.
        Respond with a JSON object containing:
        1. x: the x-coordinate (number)
        2. y: the y-coordinate (number)
        3. description: brief description of what element is at these coordinates
        """
        
        result_text = self.prompt_gemini(prompt, system_prompt, [current_state.screenshot.image])
        
        try:
            # Parse the response
            coord_data = repair_loads(result_text)
            
            # Create action event
            action_event = models.ActionEvent(
                name=action_name,
                screenshot=current_state.screenshot,
                window_event=current_state.window_event,
                recording=self.recording,
                mouse_x=coord_data.get("x", 0),
                mouse_y=coord_data.get("y", 0),
                active_segment_description=coord_data.get("description", "")
            )
            
            return action_event
            
        except Exception as e:
            logger.error(f"Error parsing coordinates: {e}")
            
            # Fallback: use center of screen
            window_width = current_state.window_event.width if current_state.window_event else 800
            window_height = current_state.window_event.height if current_state.window_event else 600
            
            return models.ActionEvent(
                name=action_name,
                screenshot=current_state.screenshot,
                window_event=current_state.window_event,
                recording=self.recording,
                mouse_x=window_width / 2,
                mouse_y=window_height / 2,
                active_segment_description="Center of screen (fallback)"
            )
    
    def _decide_between_actions(self, possible_actions, current_state):
        """Use Gemini to decide between multiple possible actions."""
        system_prompt = "You are an expert in UI automation decision making."
        
        actions_list = []
        for i, (action, next_state) in enumerate(possible_actions):
            actions_list.append({
                "id": i,
                "name": action.name,
                "description": action.description,
                "parameters": action.parameters,
                "next_state": next_state.name,
                "next_state_description": next_state.description
            })
        
        prompt = f"""
        Decide which action to take next based on the current state and task description.
        
        Task description: {self.task_description}
        Current state: {self.current_abstract_state.description if self.current_abstract_state else "Initial state"}
        
        Possible actions:
        {json.dumps(actions_list, indent=2)}
        
        Respond with a JSON object containing:
        1. chosen_action_id: the ID of the chosen action (number)
        2. reasoning: brief explanation for your choice
        """
        
        result_text = self.prompt_gemini(prompt, system_prompt, [current_state.screenshot.image])
        
        try:
            result = repair_loads(result_text)
            chosen_id = result.get("chosen_action_id", 0)
            chosen_id = min(chosen_id, len(possible_actions) - 1)  # Ensure valid index
            
            action, next_state = possible_actions[chosen_id]
            return self._instantiate_abstract_action(action, current_state)
            
        except Exception as e:
            logger.error(f"Error deciding between actions: {e}")
            # Default to first action
            action, next_state = possible_actions[0]
            return self._instantiate_abstract_action(action, current_state)
    
    def _generate_action_with_gemini(self, suggested_action_name=None):
        """Generate action with Gemini if graph doesn't provide one."""
        system_prompt = "You are an expert in UI automation."
        
        trajectory = []
        for i, (state, action) in enumerate(self.state_action_history[-5:]):
            trajectory.append({
                "step": i + 1,
                "state": state.name if state else "Unknown",
                "action": action.name if action else "None"
            })
        
        prompt = f"""
        Generate the next action to perform based on:
        
        Task description: {self.task_description}
        Recent trajectory: {json.dumps(trajectory, indent=2)}
        {f'Suggested action type: {suggested_action_name}' if suggested_action_name else ''}
        
        Analyze the screenshot and respond with a JSON object for the next ActionEvent:
        {{
            "name": "click|move|scroll|type",
            "mouse_x": number,
            "mouse_y": number,
            "text": "text to type (for keyboard actions)",
            "active_segment_description": "description of what's being clicked"
        }}
        
        Only include relevant fields based on the action type.
        """
        
        result_text = self.prompt_gemini(
            prompt, 
            system_prompt, 
            [self.current_state.screenshot.image] if self.current_state else []
        )
        
        try:
            action_dict = repair_loads(result_text)
            action = models.ActionEvent.from_dict(action_dict)
            
            # Add missing context
            action.screenshot = self.current_state.screenshot if self.current_state else None
            action.window_event = self.current_state.window_event if self.current_state else None
            action.recording = self.recording
            
            return action
            
        except Exception as e:
            logger.error(f"Error generating action: {e}")
            
            # Create a fallback action - simple click in the center
            window_width = self.current_state.window_event.width if self.current_state and self.current_state.window_event else 800
            window_height = self.current_state.window_event.height if self.current_state and self.current_state.window_event else 600
            
            return models.ActionEvent(
                name="click",
                screenshot=self.current_state.screenshot if self.current_state else None,
                window_event=self.current_state.window_event if self.current_state else None,
                recording=self.recording,
                mouse_x=window_width / 2,
                mouse_y=window_height / 2,
                mouse_button_name="left",
                active_segment_description="Center of screen (fallback)"
            )
    
    def prompt_gemini(self, prompt, system_prompt, images):
        """Helper method to prompt Gemini with images."""
        from openadapt.drivers import google
        return google.prompt(
            prompt, 
            system_prompt=system_prompt, 
            images=images, 
            model_name="models/gemini-1.5-pro-latest"
        )
    
    def __del__(self):
        """Clean up OmniParser service when done."""
        try:
            self.omniparser_provider.stack.stop_service()
        except:
            pass