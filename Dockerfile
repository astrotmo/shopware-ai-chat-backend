# syntax=docker/dockerfile:1

# Use the official Astral UV image as the base image (includes uv and Python 3.12)
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Setup ENV variables for Python and UV
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_TOOL_BIN_DIR=/usr/local/bin
# PYTHONDONTWRITEBYTECODE=1 Prevents Python from writing pyc files.
# PYTHONUNBUFFERED=1 Keeps Python from buffering stdout and stderr to avoid situations where the application crashes without emitting any logs due to buffering.
# UV_COMPILE_BYTECODE=1 Enables bytecode compilation for faster startup times.
# UV_LINK_MODE=copy Ensures that files are copied from the cache instead of linked, which is important for mounted volumes.
# UV_TOOL_BIN_DIR=/usr/local/bin Ensures that installed tools can be executed out of the box.

# Set the working directory inside the container
WORKDIR /app

# Create venv outside of /app to avoid it being included in bind mounts
RUN python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV UV_PROJECT_ENVIRONMENT=/opt/venv

# Create a non-privileged user that the app will run under.
ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/root" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

# Download dependencies as a separate step to take advantage of Docker's caching.
# Leverage a cache mount to /root/.cache/pip to speed up subsequent builds.
# Leverage a bind mount to requirements.txt to avoid having to copy them into
# into this layer.
RUN --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

# Copy the source code into the container.
COPY . /app

# Install project into the venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# Switch to the non-privileged user to run the application.
USER appuser

# Expose the port that the application listens on.
EXPOSE 8002

EXPOSE 8005

# Run the application.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002"]
