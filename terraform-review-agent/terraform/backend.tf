terraform {
  backend "s3" {
    bucket       = "my-aws-bucket-backend"
    key          = "ecs/serverless/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
    encrypt      = true
  }
}