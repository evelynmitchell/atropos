# Trajectory Handler API

## Overview

The AtroposLib API is a FastAPI application designed to act as a central buffer and aggregator for reinforcement learning (RL) experience data. Its primary purpose is to decouple RL data generation (by "Rollout Handlers" or "Environments") from RL data consumption (by one or more "Trainers"), particularly in distributed online RL settings.

This service specifically handles the **experience data pathway**:

* Rollout Handlers connect and push trajectories (tokens, masks, scores, etc.).
* The API buffers this data in a queue.
* Trainers connect and pull processed batches of experience data for training updates.

**Important:** This service does *not* handle the distribution of updated policies from the Trainer back to the Rollout Handlers/Inference Servers. That part of the online RL loop is assumed to be handled by a separate mechanism.

## Features

* Centralized, in-memory queue for RL trajectory data.
* Registration endpoints for Trainers and Rollout Handlers.
* Serves batches of aggregated experience data to Trainers.
* Supports heterogeneous environments with weighting (via `/register-env` weight and internal batching).
* Minimum batch allocation support to guarantee certain environments contribute a minimum proportion to each batch.
* Provides status endpoints for monitoring queue size and training step count.
* Basic integration with Weights & Biases (W&B) project/group info.
* Endpoints for Rollout Handlers to disconnect gracefully.
* Debug endpoint to retrieve the latest submitted data sample.

## Architecture Context

This API typically sits within a larger RL system:

1.  **Rollout Handlers:** Instances simulating the environment. They interact with Inference Servers to get actions based on the current policy and send resulting trajectory data (`ScoredData`) to this AtroposLib API (`/scored_data`).
2.  **Inference Servers (External):** Serve the current policy (e.g., via an OpenAI-compatible API). Receive policy updates directly from the Trainer. *Not part of this service.*
3.  **AtroposLib API (This Service):** Buffers and batches experience data received from Rollout Handlers.
4.  **Trainer(s):** Pull batches of experience data from this API (`/batch`), compute gradients, update the policy, and push updated policies directly to the Inference Servers.


## Running the Server

with the repository installed we provide a helper script to run the server:

```bash
run-api
```
if you need more control over the server you can run it directly with:

```bash
uvicorn atroposlib.api.server:app --host 0.0.0.0 --port 8000 --reload
```

* `--host 0.0.0.0`: Makes the server accessible on your network.
* `--port 8000`: Specifies the port (change if needed).
* `--reload`: Enables auto-reloading on code changes (for development). Remove for production.

The API documentation (Swagger UI) will be available at `http://<your-server-ip>:8000/docs`.

## API Endpoints

### General

* `GET /`
    * **Description:** Root endpoint for basic health check.
    * **Response:** `{"message": "AtroposLib API"}`

### Trainer Registration & Info

* `POST /register`
    * **Description:** Called once by the Trainer process to initialize the server state for a training run. Resets state if called again.
    * **Request Body:** `Registration` model
        ```python
        class Registration(BaseModel):
            wandb_group: str
            wandb_project: str
            batch_size: int
            max_token_len: int # Max token length expected in trajectories
            checkpoint_dir: str # Shared location for checkpoints
            save_checkpoint_interval: int
            starting_step: int
            num_steps: int # Total expected training steps
        ```
    * **Response:** `{"uuid": <generated_uuid_int>}`
* `GET /wandb_info`
    * **Description:** Retrieve W&B group and project info set during registration.
    * **Response:** `{"group": <group_name_or_null>, "project": <project_name_or_null>}`
* `GET /info`
    * **Description:** Retrieve batch size and max token length set during registration.
    * **Response:** `{"batch_size": <size_or_-1>, "max_token_len": <len_or_-1>}`
* `GET /status`
    * **Description:** Get the current training step (based on batches served) and queue size.
    * **Response:** `{"current_step": <step_count>, "queue_size": <queue_length>}`

### Rollout Handler Registration & Info

* `POST /register-env`
    * **Description:** Called by each Rollout Handler instance to register itself.
    * **Request Body:** `RegisterEnv` model
        ```python
        class RegisterEnv(BaseModel):
            max_token_length: int # Max length this env produces
            desired_name: str # Base name for identification/logging
            weight: float # Weight for sampling/batching (e.g., 1.0)
            group_size: int # Expected number of sequences per data submission
            min_batch_allocation: Optional[float] = None # Minimum proportion of batch (0.0-1.0)
        ```
    * **Response:** Provides assigned ID, unique W&B name, checkpoint info.
        ```json
        {
          "status": "success",
          "env_id": <assigned_env_id_int>,
          "wandb_name": <generated_unique_name>,
          "checkpoint_dir": <checkpoint_dir_from_registration>,
          "starting_step": <current_server_step>,
          "checkpoint_interval": <interval_from_registration>,
          "num_steps": <num_steps_from_registration>
        }
        ```
* `POST /disconnect-env`
    * **Description:** Allows a Rollout Handler to signal it's disconnecting gracefully.
    * **Request Body:** `EnvIdentifier` model `{"env_id": <registered_env_id_int>}`
    * **Response:** `{"status": "success"}` or `{"status": "failure", "error": ...}`
* `GET /status-env`
    * **Description:** Called by a Rollout Handler to get general status plus its calculated sampling weight relative to other connected environments.
    * **Query Parameter:** Requires `env: EnvIdentifier` model (e.g., `?env_id=0` - actual implementation might differ slightly, check FastAPI docs for query parameter models). **Note:** The code shows `env: EnvIdentifier` as a body parameter for a GET request, which is non-standard. This might need adjustment or testing. Assuming it works via query or a POST instead.
    * **Response:** `{"current_step": <step>, "queue_size": <size>, "env_weight": <calculated_weight_float>}`

### Data Handling

* `POST /scored_data`
    * **Description:** Endpoint for Rollout Handlers to push a single chunk of trajectory data.
    * **Request Body:** `ScoredData` model
        ```python
        class ScoredData(BaseModel):
            tokens: List[List[int]]
            masks: List[List[int]]
            scores: List[float]
            advantages: Optional[List[List[float]]] = None
            ref_logprobs: Optional[List[List[float]]] = None
            messages: Optional[List[List[Message]]] = None
            overrides: Optional[List[dict]] = None # Per-item logging overrides
            group_overrides: Optional[dict] = None # Group logging overrides
            images: Optional[Any] = None # Image data (if applicable)
            env_id: Optional[int] = None # ID of the environment that generated this data
        ```
    * **Response:**
        * Normal submission: `{"status": "received"}`
        * Mixed-size group buffered: `{"status": "buffered", "buffer_size": <sequences_in_buffer>}`
* `POST /scored_data_list`
    * **Description:** Endpoint for Rollout Handlers to push a list of `ScoredData` chunks.
    * **Request Body:** `List[ScoredData]`
    * **Response:** `{"status": "received", "groups_processed": <count>}`
* `GET /batch`
    * **Description:** Called by the Trainer to request a batch of data for training. The server uses internal logic to form a batch of the configured size from the available data in the queue. If any environments have minimum batch allocations specified, it uses `grab_batch_with_minimum_allocations` to ensure each environment gets at least its minimum proportion of the batch. Otherwise, it uses `grab_exact_from_heterogeneous_queue` to form batches respecting environment weights. The server increments its internal step counter when a batch is successfully formed and returned.
    * **Response:**
        * Success: `{"batch": [<data_item_1>, ..., <data_item_N>]}` where each `data_item` matches the structure pushed via `/scored_data`.
        * Not enough data: `{"batch": null}`
* `GET /latest_example`
    * **Description:** Debug endpoint to retrieve the most recently added `ScoredData` item.
    * **Response:** The last `ScoredData` dictionary pushed, or empty lists for tokens, masks, scores, advantages, ref_logprobs, messages, and images if none yet.

### Debugging

* `GET /reset_data`
    * **Description:** **Warning:** Resets all server state, including the queue, configuration, registered environments, and step count. Use with caution during development/debugging.
    * **Response:** Plain text `Reset successful` with HTTP status 200.

## Common Workflow Example

1.  **Start Server:** Launch the `AtroposLib` API server.
2.  **Trainer Initialization:** The main Trainer process sends a `POST /register` request with run parameters.
3.  **Rollout Handler Initialization:** Each Rollout Handler starts and sends `POST /register-env`.
4.  **Data Generation:** Handlers run simulations, collect data, and send `POST /scored_data` or `POST /scored_data_list` periodically.
5.  **Training Loop:**
    * The Trainer (e.g., Rank 0 in distributed setup) enters a loop:
        * Calls `GET /batch`.
        * If `batch` is not `null`:
            * (Distribute batch to other ranks if applicable).
            * Perform training step.
            * Optionally call `GET /status` for monitoring.
        * If `batch` is `null`:
            * Wait briefly (`time.sleep`) and retry `GET /batch`.
        * mermaid diagram of how a trainer interacts with the api is located [here](trainer_interaction.md).
    * (In distributed setups, other ranks (1..N-1) might poll `GET /status` to wait for the step counter to increment before expecting the broadcasted batch from Rank 0).
    * The envs periodically poll `GET /status-env` to check their status and sampling weight.
        * In asynchronous setups, they may stop at a maximum off-policy step count.
        * mermaid diagram of how a rollout handler interacts with the api is located [here](env_interaction.md).
6.  **Shutdown:** Handlers may call `POST /disconnect-env`.

## Minimum Batch Allocation Feature

The API supports ensuring minimum batch allocations for specific environments. This feature is useful when you want to guarantee that certain environments contribute at least a minimum proportion of sequences to each training batch.

### How It Works

1. **Environment Registration**: When registering an environment via `/register-env`, you can specify:
   - `min_batch_allocation` (Optional[float]): A value between 0.0 and 1.0 representing the minimum proportion of the batch this environment should contribute
   - `group_size` (int): The expected number of sequences per data submission from this environment

2. **Batch Formation**: When the trainer requests a batch via `/batch`:
   - If any environment has a `min_batch_allocation` specified, the system uses special logic to ensure minimums are met
   - The system attempts to allocate at least `min_batch_allocation * batch_size` sequences from each environment with a minimum
   - If the sum of all minimum allocations exceeds 1.0, they are proportionally scaled down
   - If an environment with a minimum allocation has no data available, the batch formation fails (returns null)

3. **Mixed-Size Group Handling**: When an environment submits data with a different number of sequences than its declared `group_size`:
   - The data is buffered separately for that environment
   - The system attempts to combine buffered groups to match the expected `group_size`
   - Once combined, the data is added to the main queue
   - Response includes `{"status": "buffered", "buffer_size": <sequences_in_buffer>}`

### Example Configuration

```python
# Environment 1: Requires at least 30% of each batch
{
    "max_token_length": 512,
    "desired_name": "critical_env",
    "weight": 1.0,
    "group_size": 4,
    "min_batch_allocation": 0.3  # 30% minimum
}

# Environment 2: No minimum requirement
{
    "max_token_length": 512,
    "desired_name": "standard_env",
    "weight": 1.0,
    "group_size": 2,
    "min_batch_allocation": None  # No minimum
}
```

### Important Notes

- Minimum allocations are enforced per batch, not globally
- If minimum allocations cannot be satisfied (e.g., not enough data from a required environment), batch formation fails
- Environments without `min_batch_allocation` fill the remaining batch space after minimums are satisfied
- The feature respects heterogeneous packing constraints when forming batches

## Limitations & TODOs

* **In-Memory State:** The primary limitation is that all queues, configurations, and states are stored in the FastAPI application's memory (`app.state`).
    * **No Persistence:** Data is lost if the server restarts.
    * **Scalability Bottleneck:** API cannot scale beyond a single server instance easily.
