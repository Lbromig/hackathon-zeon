# Vue 3 + Vite dev server.
#
# Runs `vite dev`, not a production build: this is an internal bench tool and hot
# reload is the point. src/ is bind-mounted at run time; node_modules stays in the
# image (a named volume masks the host's, so a macOS host and a Linux container
# never share incompatible native binaries).
FROM node:22-alpine

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./

EXPOSE 5173
# --host binds 0.0.0.0 so the port is reachable from outside the container.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
