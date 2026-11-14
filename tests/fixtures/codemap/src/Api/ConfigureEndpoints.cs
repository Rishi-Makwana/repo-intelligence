namespace Example.Api;

public static class ConfigureEndpoints
{
    public static void AddEndpoints(this WebApplication app)
    {
        var apiEndpoints = app
            .MapGroup("/api");

        var authenticatedEndpoints = apiEndpoints.MapGroup("")
            .RequireAuthorization();

        GetResource.Map(authenticatedEndpoints);
        HealthEndpoints.Map(apiEndpoints);
    }
}
