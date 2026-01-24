#!/usr/bin/env bash
# build.sh - Prepare the application for deployment
# Usage (Render): set this script as the "Build Command"

# Exit immediately if a command exits with a non-zero status
set -o errexit

echo "🚀 Starting Build Process..."

# 1. Upgrade pip to ensure smooth installation
pip install --upgrade pip

# 2. Install dependencies
echo "📦 Installing dependencies..."
pip install -r requirements.txt

# 3. (Optional) Compile assets or other build steps here
# e.g., npm install && npm run build (if you had a frontend build)

echo "✅ Build Completed Successfully!"
