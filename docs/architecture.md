# OpenAdapt Architecture

OpenAdapt v1.0+ uses a **modular meta-package architecture** where the main `openadapt` package provides a unified CLI and depends on focused sub-packages.

## System Overview

```mermaid
flowchart TB
    subgraph User["User"]
        UI[Desktop/Web GUI]
    end

    subgraph OpenAdapt["OpenAdapt Meta-Package"]
        CLI[openadapt CLI]
        LAZY[Lazy Imports]
    end

    subgraph Core["Core Packages"]
        CAPTURE[openadapt-capture]
        ML[openadapt-ml]
        EVALS[openadapt-evals]
        VIEWER[openadapt-viewer]
    end

    subgraph Optional["Optional Packages"]
        GROUNDING[openadapt-grounding]
        RETRIEVAL[openadapt-retrieval]
        PRIVACY[openadapt-privacy]
    end

    subgraph Storage["Storage"]
        DEMO[(Demonstration<br/>JSON/Parquet)]
        MODEL[(Model<br/>Checkpoints)]
        RESULTS[(Evaluation<br/>Results)]
    end

    %% User interactions
    UI --> CAPTURE

    %% CLI orchestration
    CLI --> CAPTURE
    CLI --> ML
    CLI --> EVALS
    CLI --> VIEWER

    %% Lazy loading
    LAZY -.-> GROUNDING
    LAZY -.-> RETRIEVAL
    LAZY -.-> PRIVACY

    %% Data flow
    CAPTURE --> DEMO
    DEMO --> ML
    ML --> MODEL
    MODEL --> EVALS
    EVALS --> RESULTS
    DEMO --> VIEWER

    %% Optional integrations
    GROUNDING -.-> ML
    RETRIEVAL -.-> ML
    PRIVACY -.-> CAPTURE
    PRIVACY -.-> VIEWER

    classDef metaPkg fill:#4A90D9,stroke:#2E5A8B,color:#fff
    classDef corePkg fill:#5CB85C,stroke:#3D7A3D,color:#fff
    classDef optPkg fill:#F0AD4E,stroke:#C79121,color:#fff
    classDef storage fill:#9B59B6,stroke:#6C3483,color:#fff
    classDef user fill:#E74C3C,stroke:#A93226,color:#fff

    class CLI,LAZY metaPkg
    class CAPTURE,ML,EVALS,VIEWER corePkg
    class GROUNDING,RETRIEVAL,PRIVACY optPkg
    class DEMO,MODEL,RESULTS storage
    class UI user
```

## Data Flow Pipeline

```mermaid
flowchart LR
    subgraph Record["1. Record"]
        A[User Demo] --> B[Capture Session]
        B --> C[Screenshots + Events]
    end

    subgraph Store["2. Store"]
        C --> D[JSON/Parquet Files]
        D --> E[Demo Library]
    end

    subgraph Train["3. Train"]
        E --> F[Data Loading]
        F --> G[Model Training]
        G --> H[Checkpoint]
    end

    subgraph Deploy["4. Deploy"]
        H --> I[Agent Policy]
        I --> J[Inference]
        J --> K[Action Replay]
    end

    subgraph Evaluate["5. Evaluate"]
        I --> L[Benchmark Runner]
        L --> M[Metrics]
        M --> N[Results Report]
    end

    %% Optional enhancements
    GROUND[Grounding] -.-> J
    RETRIEVE[Retrieval] -.-> F
    PRIV[Privacy] -.-> C

    classDef phase fill:#3498DB,stroke:#1A5276,color:#fff
    classDef optional fill:#F39C12,stroke:#B7950B,color:#fff

    class A,B,C,D,E,F,G,H,I,J,K,L,M,N phase
    class GROUND,RETRIEVE,PRIV optional
```

## Package Dependencies

```mermaid
graph TD
    OA[openadapt<br/>Meta-package]

    OA -->|capture| CAP[openadapt-capture]
    OA -->|ml| MLP[openadapt-ml]
    OA -->|evals| EVL[openadapt-evals]
    OA -->|viewer| VWR[openadapt-viewer]
    OA -->|grounding| GRD[openadapt-grounding]
    OA -->|retrieval| RET[openadapt-retrieval]
    OA -->|privacy| PRV[openadapt-privacy]

    %% Core bundle
    OA -->|core| CORE[Core Bundle]
    CORE --> CAP
    CORE --> MLP
    CORE --> EVL
    CORE --> VWR

    %% All bundle
    OA -->|all| ALL[Full Bundle]
    ALL --> CORE
    ALL --> GRD
    ALL --> RET
    ALL --> PRV

    classDef meta fill:#2C3E50,stroke:#1A252F,color:#fff
    classDef core fill:#27AE60,stroke:#1E8449,color:#fff
    classDef optional fill:#E67E22,stroke:#A04000,color:#fff
    classDef bundle fill:#8E44AD,stroke:#5B2C6F,color:#fff

    class OA meta
    class CAP,MLP,EVL,VWR core
    class GRD,RET,PRV optional
    class CORE,ALL bundle
```

## Component Details

### Core Packages

| Package | Responsibility | Key Exports |
|---------|---------------|-------------|
| **openadapt-capture** | GUI recording, event capture, storage | `CaptureSession`, `Recorder`, `Action` |
| **openadapt-ml** | Model training, inference, adapters | `QwenVLAdapter`, `Trainer`, `AgentPolicy` |
| **openadapt-evals** | Benchmark evaluation, metrics | `ApiAgent`, `BenchmarkAdapter`, `evaluate_agent_on_benchmark` |
| **openadapt-viewer** | HTML visualization, replay viewer | `PageBuilder`, `HTMLBuilder` |

### Optional Packages

| Package | Responsibility | Use Case |
|---------|---------------|----------|
| **openadapt-grounding** | UI element localization | Improved click accuracy with element detection |
| **openadapt-retrieval** | Multimodal demo search | Find similar demonstrations for few-shot prompting |
| **openadapt-privacy** | PII/PHI scrubbing | Redact sensitive data before storage/training |

## Evaluation Loop

```mermaid
flowchart TB
    subgraph Agent["Agent Under Test"]
        POLICY[Agent Policy]
        API[API Agent<br/>Claude/GPT]
    end

    subgraph Benchmark["Benchmark System"]
        ADAPTER[Benchmark Adapter]
        MOCK[Mock Adapter]
        LIVE[Live WAA Adapter]
    end

    subgraph Tasks["Task Execution"]
        TASK[Get Task]
        OBS[Observe State]
        ACT[Execute Action]
        CHECK[Check Success]
    end

    subgraph Metrics["Metrics"]
        SUCCESS[Success Rate]
        STEPS[Avg Steps]
        TIME[Execution Time]
    end

    POLICY --> ADAPTER
    API --> ADAPTER
    ADAPTER --> MOCK
    ADAPTER --> LIVE

    MOCK --> TASK
    LIVE --> TASK
    TASK --> OBS
    OBS --> POLICY
    OBS --> API
    POLICY --> ACT
    API --> ACT
    ACT --> CHECK
    CHECK -->|next| TASK
    CHECK -->|done| SUCCESS
    CHECK --> STEPS
    CHECK --> TIME

    classDef agent fill:#3498DB,stroke:#1A5276,color:#fff
    classDef bench fill:#2ECC71,stroke:#1E8449,color:#fff
    classDef task fill:#9B59B6,stroke:#6C3483,color:#fff
    classDef metric fill:#E74C3C,stroke:#A93226,color:#fff

    class POLICY,API agent
    class ADAPTER,MOCK,LIVE bench
    class TASK,OBS,ACT,CHECK task
    class SUCCESS,STEPS,TIME metric
```

## CLI Command Structure

```mermaid
graph LR
    OA[openadapt]

    OA --> CAP[capture]
    OA --> TRN[train]
    OA --> EVL[eval]
    OA --> SRV[serve]
    OA --> VER[version]
    OA --> DOC[doctor]

    CAP --> CS[start]
    CAP --> CT[stop]
    CAP --> CL[list]
    CAP --> CV[view]

    TRN --> TS[start]
    TRN --> TST[status]
    TRN --> TSP[stop]

    EVL --> ER[run]
    EVL --> EM[mock]

    classDef root fill:#2C3E50,stroke:#1A252F,color:#fff
    classDef group fill:#3498DB,stroke:#1A5276,color:#fff
    classDef cmd fill:#27AE60,stroke:#1E8449,color:#fff

    class OA root
    class CAP,TRN,EVL,SRV,VER,DOC group
    class CS,CT,CL,CV,TS,TST,TSP,ER,EM cmd
```

## Installation Options

```bash
# Minimal CLI only
pip install openadapt

# Individual packages
pip install openadapt[capture]     # GUI capture/recording
pip install openadapt[ml]          # ML training and inference
pip install openadapt[evals]       # Benchmark evaluation
pip install openadapt[viewer]      # HTML visualization

# Optional packages
pip install openadapt[grounding]   # UI element localization
pip install openadapt[retrieval]   # Demo search/retrieval
pip install openadapt[privacy]     # PII/PHI scrubbing

# Bundles
pip install openadapt[core]        # capture + ml + evals + viewer
pip install openadapt[all]         # Everything
```

---

*This architecture enables independent development and versioning of each component while maintaining a unified CLI experience.*
