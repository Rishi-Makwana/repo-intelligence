namespace Example.Api.Endpoints;

public abstract class GetResource : IEndpoint
{
    public static void Map(IEndpointRouteBuilder app) => app
        .MapGet("/resources/{resourceId:guid}", Handle)
        .WithName("GetResource");

    private static async Task<IResult> Handle(Guid resourceId)
    {
        return TypedResults.Ok();
    }
}

public static class HealthEndpoints
{
    public static void Map(IEndpointRouteBuilder app)
    {
        app.MapPost("/ping", () => TypedResults.Ok());
    }
}
