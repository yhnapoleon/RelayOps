# Build Stage
FROM node:22-alpine AS build

WORKDIR /app

# Copy package info
COPY package.*json ./

# Install dependencies
RUN npm ci

# Copy source and build
COPY . .
RUN npm run build

# Production Stage
FROM nginx:alpine

# Copy built assets from build stage to nginx web root
COPY --from=build /app/dist /usr/share/nginx/html

# Replace default Nginx configuration with our custom one
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Expose port (Nginx default is 80)
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
